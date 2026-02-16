import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, date
from typing import Dict, Optional, Any, List

from pyopenttdadmin import Admin, openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.enums import Actions, ChatDestTypes

COMPANY_RE = re.compile(r"#:\s*(\d+)(?:\([^)]*\))?\s+Company Name:\s*'([^']*)'\s+Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I)
CLIENT_RE = re.compile(r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)", re.I)

SPECTATOR_ID = 255
BROADCAST_INTERVAL = 3600
RCON_TIMEOUT = 5
DATE_POLL_INTERVAL = 3
STATE_POLL_INTERVAL = 60
DATE_STALE_THRESHOLD = 5
MESSAGE_DELAY = 0.05
GREETING_DELAY = 5
COMMAND_COOLDOWN = 2
RESET_CONFIRM_TIMEOUT = 15
COOLDOWN_CLEANUP_INTERVAL = 300
RECONNECT_DELAY = 15

def fmt(v: int) -> str:
    """Format integer with abbreviated suffixes (b/m/k)."""
    for t, s in [(1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")]:
        if v >= t: return f"{v/t:.1f}{s}"
    return str(v)

def parse_int(s: str) -> int:
    """Parse integer from string, removing commas."""
    return int(s.replace(',', ''))

class Bot:
    def __init__(self, cfg: Dict[str, Any], log: logging.Logger):
        """Initialize bot with config and logger."""
        self.cfg, self.log = cfg, log
        self.admin: Optional[Admin] = None
        self.running = False
        self._lock = threading.RLock()
        self.companies: Dict[int, Dict[str, Any]] = {}
        self.clients: Dict[int, Dict[str, Any]] = {}
        self.game_year = 0
        self.game_date: Optional[int] = None
        self.last_date_ts: Optional[float] = None
        self.last_date_change: Optional[float] = None
        self.goal_reached = False
        self.cooldowns: Dict[int, float] = {}
        self.reset_pending: Dict[int, int] = {}
        self.last_pause_cmd: Optional[bool] = None
        self.last_cmd_time = 0.0
        self.connection_errors = 0

    def rcon(self, cmd: str, timeout: float = RCON_TIMEOUT) -> str:
        """Execute RCON command and return response."""
        if not self.admin: 
            raise RuntimeError("Admin connection not initialized")
        
        self.log.debug(f"RCON> {cmd}")
        
        try:
            self.admin.send_rcon(cmd)
        except Exception as e:
            self.log.error(f"Failed to send RCON command '{cmd}': {e}")
            raise
        
        buf, done, deadline = [], False, time.time() + timeout
        
        while not done and time.time() < deadline:
            try:
                for pkt in self.admin.recv():
                    if isinstance(pkt, openttdpacket.RconPacket): 
                        buf.append(pkt.response.strip())
                    elif isinstance(pkt, openttdpacket.RconEndPacket): 
                        done = True
                        break
                    else: 
                        self.admin.handle_packet(pkt)
            except Exception as e:
                self.log.warning(f"Error receiving RCON response: {e}")
                break
        
        if not done: 
            raise TimeoutError(f"RCON command '{cmd}' timed out after {timeout}s")
        
        result = '\n'.join(buf)
        self.log.debug(f"RCON< {result[:200]}")
        return result

    def msg(self, text: str, cid: Optional[int] = None) -> None:
        """Send message to client or broadcast."""
        if not self.admin: 
            return
        
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            try:
                dest = (ChatDestTypes.CLIENT, cid) if cid else (ChatDestTypes.BROADCAST, 0)
                self.admin._chat(line, Actions.SERVER_MESSAGE, dest[0], dest[1])
                time.sleep(MESSAGE_DELAY)
            except Exception as e: 
                self.log.error(f"Failed to send message '{line[:50]}...': {e}")

    def send_poll(self, update_type: int, data: int = 0) -> None:
        """Send manual poll request for updates."""
        if not self.admin: return
        PACKET_TYPE = 3
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet_size = 3 + len(payload)
        packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
        self.admin.socket.sendall(packet)

    def poll_state(self) -> bool:
        """Poll server state via RCON."""
        try:
            co_out = self.rcon("companies")
            cl_out = self.rcon("clients")
            
            with self._lock:
                self.companies.clear()
                for m in COMPANY_RE.finditer(co_out):
                    cid, name, year, _, _, value = m.groups()
                    try:
                        self.companies[int(cid)] = {
                            'name': name.strip(),
                            'founded': int(year),
                            'value': parse_int(value)
                        }
                    except ValueError as e:
                        self.log.warning(f"Failed to parse company data: {e}")
                
                self.clients.clear()
                for m in CLIENT_RE.finditer(cl_out):
                    cid, name, co = m.groups()
                    try:
                        self.clients[int(cid)] = {
                            'name': name.strip(),
                            'company_id': int(co)
                        }
                    except ValueError as e:
                        self.log.warning(f"Failed to parse client data: {e}")
            
            self.connection_errors = 0
            self.log.info(f"Poll: {len(self.companies)} companies, {len(self.clients)} clients, year {self.game_year}")
            return True
            
        except Exception as e:
            self.connection_errors += 1
            self.log.error(f"Poll error (attempt {self.connection_errors}): {e}")
            return False

    def build_cv(self) -> str:
        """Build company value leaderboard."""
        with self._lock:
            if not self.companies: return "No companies"
            sorted_cos = sorted(self.companies.items(), key=lambda x: x[1].get('value', 0), reverse=True)[:10]
            lines = ["=== Company Value Rankings ==="]
            lines.extend(f"{i}. {d['name']}: {fmt(d['value'])}" for i, (_, d) in enumerate(sorted_cos, 1))
            return '\n'.join(lines)

    def normalize_company_id(self, raw_id: int) -> int:
        """Convert packet company ID (0-based, 255=spectator) to internal ID (1-based, 255=spectator)."""
        return SPECTATOR_ID if raw_id == SPECTATOR_ID else raw_id + 1

    def apply_pause_policy(self) -> None:
        """Apply pause/unpause based on company count."""
        with self._lock:
            should_pause = len(self.companies) == 0
            if self.last_pause_cmd == should_pause or time.time() - self.last_cmd_time < 1.0:
                return
            self.last_cmd_time = time.time()
        
        try:
            cmd = "pause" if should_pause else "unpause"
            response = self.rcon(cmd)
            if f"already {'paused' if should_pause else 'unpaused'}" not in response.lower():
                self.log.info(f"{'Paused' if should_pause else 'Unpaused'}: {'no companies' if should_pause else 'company present'}")
            with self._lock:
                self.last_pause_cmd = should_pause
        except Exception as e:
            self.log.error(f"Pause policy error: {e}")

    def reset_unnamed_co1(self) -> None:
        """Reset company #1 if unnamed and no clients."""
        try:
            with self._lock:
                co1 = self.companies.get(1)
                if not co1 or co1.get('name') != 'Unnamed':
                    return
                if any(c['company_id'] == 1 for c in self.clients.values()):
                    return
            self.rcon("reset_company 1")
            self.log.info("Reset unnamed company #1")
        except Exception as e:
            self.log.error(f"Error resetting unnamed co1: {e}")

    def auto_clean(self) -> None:
        """Auto-reset old low-value companies."""
        age_thresh = self.cfg.get('clean_age', 0)
        val_thresh = self.cfg.get('clean_value', 0)
        if age_thresh <= 0 or val_thresh <= 0: 
            return
        
        with self._lock:
            to_clean = []
            for cid, d in self.companies.items():
                age = self.game_year - d.get('founded', self.game_year)
                if age >= age_thresh and d.get('value', 0) < val_thresh:
                    clients = [c for c, cd in self.clients.items() if cd['company_id'] == cid]
                    to_clean.append((cid, d['name'], age, d['value'], clients))
        
        for cid, name, age, value, clients in to_clean:
            try:
                for c in clients:
                    self.rcon(f"move {c} {SPECTATOR_ID}")
                self.rcon(f"reset_company {cid}")
                self.msg(f"Company {name} auto-reset")
                self.log.info(f"Auto-clean: co#{cid} {name} age={age} val={value}")
            except Exception as e:
                self.log.error(f"Error auto-cleaning company #{cid}: {e}")

    def reset_state(self) -> None:
        """Reset all tracked state."""
        with self._lock:
            self.companies.clear()
            self.clients.clear()
            self.game_year = 0
            self.game_date = None
            self.last_date_ts = None
            self.last_date_change = None
            self.goal_reached = False
            self.reset_pending.clear()
            self.cooldowns.clear()
            self.last_pause_cmd = None

    def check_goal(self) -> None:
        """Check goal and trigger map reload."""
        goal = self.cfg.get('goal_value', 0)
        if self.goal_reached or goal <= 0: 
            return
        
        with self._lock:
            winner = next((d for d in self.companies.values() if d.get('value', 0) >= goal), None)
            if winner:
                self.goal_reached = True
                winner_name = winner['name']
                winner_value = winner['value']
        
        if not winner: 
            return
        
        self.log.info(f"Goal: {winner_name} reached {fmt(winner_value)}")
        self.msg(f"{winner_name} WINS! Reached {fmt(goal)}!")
        
        for t in [20, 15, 10, 5]:
            self.msg(f"Reloading in {t}s...")
            time.sleep(5)
        
        try:
            if map_file := self.cfg.get('load_map', ''):
                cmd = f"load_scenario {map_file}" if map_file.endswith('.scn') else f"load {map_file}"
                self.rcon(cmd)
                self.reset_state()
                self.send_poll(AdminUpdateType.DATE.value)
                self.poll_state()
                self.reset_unnamed_co1()
                self.apply_pause_policy()
        except Exception as e:
            self.log.error(f"Error reloading map: {e}")

    def greet(self, cid: int) -> None:
        """Greet message for new clients with a small delay after connected."""
        time.sleep(GREETING_DELAY)
        if not self.running: 
            return
        
        try:
            with self._lock:
                name = self.clients.get(cid, {}).get('name', f'Player{cid}')
                paused = self.last_date_change is None or time.time() - self.last_date_change > DATE_STALE_THRESHOLD
            
            self.log.info(f"Greeting: {name} (#{cid}, paused={paused})")
            msg = f"Welcome {name}, create a company to unpause game, type !help for commands" if paused else f"Welcome {name}, type !help for commands"
            self.msg(msg)
            
        except Exception as e:
            self.log.error(f"Error greeting client #{cid}: {e}")

    def handle_cmd(self, cid: int, text: str) -> None:
        """Process chat commands. Ignore commands when paused"""
        with self._lock:
            if self.last_date_change is None or time.time() - self.last_date_change > DATE_STALE_THRESHOLD:
                return
            if time.time() - self.cooldowns.get(cid, 0) < COMMAND_COOLDOWN: 
                return
            self.cooldowns[cid] = time.time()
        
        parts = text.split()
        if not parts: 
            return
        
        cmd = parts[0].lower()
        self.log.info(f"Command: !{cmd} from #{cid}")
        
        try:
            if cmd == "help":
                self.msg("=== Commands ===\n!info !rules !cv !reset", cid)
            elif cmd == "info":
                self.msg(f"Goal: first company to reach {fmt(self.cfg.get('goal_value', 0))} company value wins!\nGamescript: Production Booster v3\nPrimary industries(coal,wood,oil,etc) >70% transported increases, <50% decreases production", cid)
            elif cmd == "rules":
                self.msg(f"1. Be respectful\n2. No griefing or blocking industries/cities\n3. Respect other players; no sabotage\n4. Companies >{self.cfg.get('clean_age', 2)}yrs & company value <{fmt(self.cfg.get('clean_value', 1000))} auto-cleaned\n5. Enjoy, have fun!", cid)
            elif cmd == "cv":
                self.msg(self.build_cv(), cid)
            elif cmd == "reset":
                with self._lock:
                    co = self.clients.get(cid, {}).get('company_id', SPECTATOR_ID)
                    if co == SPECTATOR_ID or co not in self.companies:
                        self.msg("You must be in a company", cid)
                        return
                    self.reset_pending[cid] = co
                
                self.log.info(f"Reset: client #{cid} company #{co}")
                self.msg(f"Move to spectator in {RESET_CONFIRM_TIMEOUT}s to reset company", cid)
                
                def timeout():
                    time.sleep(RESET_CONFIRM_TIMEOUT)
                    with self._lock:
                        if self.reset_pending.pop(cid, None):
                            self.log.debug(f"Reset timeout: client #{cid}")
                            self.msg(f"Reset timeout after {RESET_CONFIRM_TIMEOUT}s", cid)
                
                threading.Thread(target=timeout, daemon=True).start()
                
        except Exception as e:
            self.log.error(f"Error handling command !{cmd}: {e}")

    def setup_handlers(self) -> None:
        """Register packet handlers."""
        if not self.admin: return

        @self.admin.add_handler(openttdpacket.ChatPacket)
        def on_chat(admin: Admin, pkt: openttdpacket.ChatPacket) -> None:
            try:
                msg = pkt.message.strip()
                cid = getattr(pkt, 'id', None)
                
                if msg.startswith('!') and cid is not None:
                    self.log.debug(f"Command received: {msg} from #{cid}")
                    self.handle_cmd(cid, msg[1:])
            except Exception as e:
                self.log.error(f"Error in on_chat handler: {e}", exc_info=True)

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        def on_client_info(admin: Admin, pkt: openttdpacket.ClientInfoPacket) -> None:
            try:
                co = self.normalize_company_id(pkt.company_id)
                with self._lock:
                    self.clients[pkt.id] = {'name': pkt.name, 'company_id': co}
                self.log.debug(f"ClientInfo: #{pkt.id} {pkt.name} co={co}")
            except Exception as e:
                self.log.error(f"Error in on_client_info: {e}")

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        def on_client_update(admin: Admin, pkt: openttdpacket.ClientUpdatePacket) -> None:
            try:
                co = self.normalize_company_id(pkt.company_id)
                with self._lock:
                    if pkt.id in self.clients:
                        self.clients[pkt.id]['company_id'] = co
                    else:
                        self.clients[pkt.id] = {'name': pkt.name, 'company_id': co}
                    pending_co = self.reset_pending.pop(pkt.id, None) if co == SPECTATOR_ID else None
                
                if pending_co:
                    self.rcon(f"reset_company {pending_co}")
                    self.msg(f"Company #{pending_co} reset", pkt.id)
                    with self._lock:
                        self.companies.pop(pending_co, None)
                        self.last_pause_cmd = None
                    self.log.info(f"Reset complete: company #{pending_co}")
                    self.poll_state()
                    self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_client_update: {e}")

        @self.admin.add_handler(openttdpacket.ClientJoinPacket)
        def on_client_join(admin: Admin, pkt: openttdpacket.ClientJoinPacket) -> None:
            threading.Thread(target=self.greet, args=[pkt.id], daemon=True).start()

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        def on_client_remove(admin: Admin, pkt: Any) -> None:
            try:
                with self._lock:
                    self.clients.pop(pkt.id, None)
                    self.reset_pending.pop(pkt.id, None)
            except Exception as e:
                self.log.error(f"Error in on_client_remove: {e}")

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        def on_company_remove(admin: Admin, pkt: openttdpacket.CompanyRemovePacket) -> None:
            try:
                cid = self.normalize_company_id(pkt.id)
                with self._lock:
                    if self.companies.pop(cid, None):
                        self.log.info(f"Company removed: #{cid}")
                self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_company_remove: {e}")

        @self.admin.add_handler(openttdpacket.CompanyInfoPacket, openttdpacket.CompanyNewPacket)
        def on_company_add(admin: Admin, pkt: Any) -> None:
            try:
                cid = self.normalize_company_id(pkt.id)
                added = False
                with self._lock:
                    if cid not in self.companies:
                        self.companies[cid] = {}
                        added = True
                if added:
                    self.log.info(f"Company added: #{cid}")
                    self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_company_add: {e}")

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        def on_new_game(admin: Admin, pkt: openttdpacket.NewGamePacket) -> None:
            try:
                self.log.info("New game detected")
                self.reset_state()
                self.send_poll(AdminUpdateType.DATE.value)
                self.poll_state()
                self.reset_unnamed_co1()
                self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_new_game: {e}")

        @self.admin.add_handler(openttdpacket.DatePacket)
        def on_date(admin: Admin, pkt: openttdpacket.DatePacket) -> None:
            try:
                now = time.time()
                with self._lock:
                    old_date = self.game_date
                    self.game_year = (date(1, 1, 1) + timedelta(days=pkt.date)).year - 1
                    self.game_date = pkt.date
                    self.last_date_ts = now
                    
                    if old_date is not None and old_date != pkt.date:
                        self.last_date_change = now
                        self.log.debug(f"Date changed: {old_date} -> {pkt.date} (year {self.game_year})")
                    elif old_date is None:
                        self.last_date_change = now
            except Exception as e:
                self.log.error(f"Error in on_date: {e}")

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        def on_shutdown(admin: Admin, pkt: openttdpacket.ShutdownPacket) -> None:
            try:
                self.running = False
                self.log.info("Server shutdown")
            except Exception as e:
                self.log.error(f"Error in on_shutdown: {e}")

    def run(self) -> None:
        """Main bot loop."""
        try:
            self.admin = Admin(ip=self.cfg['server_ip'], port=self.cfg['admin_port'])
            self.admin.login(self.cfg['admin_name'], self.cfg['admin_pass'])
            self.admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.CONSOLE, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.POLL)
            
            self.setup_handlers()
            self.send_poll(AdminUpdateType.DATE.value)
            self.poll_state()
            self.reset_unnamed_co1()
            self.apply_pause_policy()
            self.running = True
            
            self.log.info(f"Connected to {self.cfg['server_ip']}:{self.cfg['admin_port']}")
            self.msg("Admin connected")
            
        except Exception as e:
            self.log.error(f"Failed to initialize connection: {e}")
            raise
        
        next_tick = time.time() + STATE_POLL_INTERVAL
        next_broadcast = (datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
        next_date_poll = time.time() + DATE_POLL_INTERVAL
        next_cleanup = time.time() + COOLDOWN_CLEANUP_INTERVAL
        
        while self.running:
            try:
                for pkt in self.admin.recv():
                    self.admin.handle_packet(pkt)
                
                now = time.time()
                
                # Poll date every DATE_POLL_INTERVAL seconds for pause detection
                if now >= next_date_poll:
                    self.send_poll(AdminUpdateType.DATE.value)
                    next_date_poll = now + DATE_POLL_INTERVAL
                
                # Cleanup stale cooldowns every COOLDOWN_CLEANUP_INTERVAL seconds
                if now >= next_cleanup:
                    with self._lock:
                        self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < COOLDOWN_CLEANUP_INTERVAL}
                    next_cleanup = now + COOLDOWN_CLEANUP_INTERVAL
                
                # Check if game is paused
                with self._lock:
                    paused = self.last_date_change is None or now - self.last_date_change > DATE_STALE_THRESHOLD
                
                if paused:
                    time.sleep(0.2)
                    continue
                
                # Hourly state poll and maintenance
                if now >= next_tick:
                    if self.poll_state():
                        self.auto_clean()
                        self.check_goal()
                    next_tick = now + STATE_POLL_INTERVAL
                
                # CV broadcast
                if now >= next_broadcast:
                    self.msg(self.build_cv())
                    self.log.info(f"Broadcast CV every {BROADCAST_INTERVAL}s")
                    next_broadcast += BROADCAST_INTERVAL
            
            except Exception as e:
                self.log.error(f"Loop error: {e}", exc_info=True)
                time.sleep(1)
        
        self.log.info("Bot stopped")

def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate configuration and return list of errors."""
    errors = []
    
    required = ['server_ip', 'admin_name', 'admin_pass', 'clean_age', 'clean_value', 'goal_value', 'load_map']
    for field in required:
        if field not in cfg:
            errors.append(f"Missing required field: {field}")
    
    if 'server_ip' in cfg and not cfg['server_ip']:
        errors.append("server_ip cannot be empty")
    
    if 'admin_port' in cfg:
        port = cfg['admin_port']
        if not isinstance(port, int) or port < 1 or port > 65535:
            errors.append(f"Invalid admin_port: {port}")
    
    for field in ['clean_age', 'clean_value', 'goal_value']:
        if field in cfg:
            value = cfg[field]
            if not isinstance(value, int) or value < 0:
                errors.append(f"{field} must be a non-negative integer")
    
    if 'load_map' in cfg and not cfg['load_map']:
        errors.append("load_map cannot be empty")
    
    return errors

def run_bot(cfg: Dict[str, Any], log: logging.Logger) -> None:
    """Run bot with auto-reconnect."""
    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log.error(f"Config error: {error}")
        return
    
    while True:
        try:
            Bot(cfg, log).run()
            break
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            break
        except Exception as e:
            log.error(f"Bot error: {e}, reconnecting in {RECONNECT_DELAY}s...", exc_info=True)
            time.sleep(RECONNECT_DELAY)

def main() -> None:
    """Entry point."""
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    
    try:
        with open("settings.json") as f:
            settings = json.load(f)
    except FileNotFoundError:
        print("Error: settings.json not found")
        return
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in settings.json: {e}")
        return
    
    if not settings.get("admin_ports"):
        print("Error: admin_ports must be specified in settings.json")
        return
    
    log_level = logging.DEBUG if settings.get("debug") else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("Bot")
    log.info("=== OpenTTD Admin Bot Starting ===")
    
    threads = []
    for port in settings["admin_ports"]:
        if not isinstance(port, int):
            log.error(f"Invalid port: {port}")
            continue
        cfg = {**settings, "admin_port": port}
        t = threading.Thread(target=run_bot, args=(cfg, logging.getLogger(f"Bot[{port}]")), daemon=True)
        t.start()
        threads.append(t)
    
    if not threads:
        log.error("No valid servers")
        return
    
    log.info(f"Started {len(threads)} bot thread(s)")
    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
