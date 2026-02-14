import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any

from pyopenttdadmin import Admin, openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.enums import Actions, ChatDestTypes

COMPANY_RE = re.compile(
    r"#\s*:?(\d+)(?:\([^)]*\))?\s+Company Name:\s*'([^']*)'\s+"
    r"Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+"
    r"Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I)
CLIENT_RE = re.compile(r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)", re.I)
DATE_RE = re.compile(r"Date:\s*(\d{4})-\d{2}-\d{2}")

SPECTATOR_ID = 255
RCON_TIMEOUT = 5
POLL_INTERVAL = 60
MESSAGE_DELAY = 0.05
GREETING_DELAY = 5
COMMAND_COOLDOWN = 3
RESET_CONFIRM_TIMEOUT = 10
RECONNECT_DELAY = 10


def fmt(v: int) -> str:
    """Convert a number to a k/m/b suffixed string, choosing the largest matching threshold."""
    for threshold, suffix in [(1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")]:
        if v >= threshold:
            return f"{v/threshold:.1f}{suffix}"
    return str(v)


def parse_int(s: str) -> int:
    """Parse integer from string, removing commas."""
    return int(s.replace(',', ''))


class Bot:
    def __init__(self, cfg: Dict[str, Any], log: logging.Logger):
        """Store shared config/logger and initialize runtime state containers and locks."""
        self.cfg = cfg
        self.log = log
        self.admin: Optional[Admin] = None
        self.running = False
        self._lock = threading.RLock()
        self.companies: Dict[int, Dict[str, Any]] = {}
        self.clients: Dict[int, Dict[str, Any]] = {}
        self.game_year = 0
        self.goal_reached = False
        self.cooldowns: Dict[int, float] = {}
        self.reset_pending: Dict[int, int] = {}
        self.paused = False
        self.last_poll_success = 0.0
        self.connection_errors = 0

    def rcon(self, cmd: str, timeout: float = RCON_TIMEOUT) -> str:
        """Send an RCON command, collect line responses until the end packet or timeout, and pass through other packets."""
        if not self.admin:
            raise RuntimeError("Admin connection not initialized")
        
        self.log.debug(f"RCON> {cmd}")
        try:
            self.admin.send_rcon(cmd)
        except Exception as e:
            self.log.error(f"Failed to send RCON command '{cmd}': {e}")
            raise
        
        buf: List[str] = []
        done = False
        deadline = time.time() + timeout
        
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
        
        result = '\n'.join(buf)
        self.log.debug(f"RCON< {result[:200]}")
        
        if not done:
            self.log.warning(f"RCON command '{cmd}' timed out after {timeout}s")
        
        return result

    def msg(self, text: str, cid: Optional[int] = None) -> None:
        """Send each non-empty line as a chat message (broadcast or to a client) with a brief delay to avoid flooding."""
        if not self.admin:
            return
        
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            try:
                if cid is not None:
                    self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT, cid)
                else:
                    self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.BROADCAST, 0)
                time.sleep(MESSAGE_DELAY)
            except Exception as e:
                self.log.error(f"Failed to send message '{line[:50]}': {e}")

    def poll_rcon(self) -> bool:
        """Refresh companies, clients, and game year via RCON, parsing regex outputs into in-memory maps under lock. Returns True on success."""
        try:
            co_out = self.rcon("companies")
            cl_out = self.rcon("clients")
            dt_out = self.rcon("get_date")
            
            with self._lock:
                self.companies.clear()
                for m in COMPANY_RE.finditer(co_out):
                    cid, name, year, money, loan, value = m.groups()
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
                    client_id, name, company_id = m.groups()
                    try:
                        self.clients[int(client_id)] = {
                            'name': name.strip(),
                            'company_id': int(company_id)
                        }
                    except ValueError as e:
                        self.log.warning(f"Failed to parse client data: {e}")
                
                dm = DATE_RE.search(dt_out)
                if dm:
                    self.game_year = int(dm.group(1))
            
            self.last_poll_success = time.time()
            self.connection_errors = 0
            self.log.debug(f"Poll: {len(self.companies)} cos, {len(self.clients)} cls, year={self.game_year}")
            return True
            
        except Exception as e:
            self.connection_errors += 1
            self.log.error(f"Error in poll_rcon (attempt {self.connection_errors}): {e}")
            return False

    def is_game_paused(self) -> bool:
        """Query game date twice 3s apart; if unchanged, treat the game as paused, else running."""
        try:
            d1 = DATE_RE.search(self.rcon("get_date"))
            time.sleep(3)
            d2 = DATE_RE.search(self.rcon("get_date"))
            return bool(d1 and d2 and d1.group(0) == d2.group(0))
        except Exception as e:
            self.log.warning(f"Failed to check pause state: {e}")
            return False

    def build_cv(self) -> str:
        """Return a top-10 leaderboard of company values (descending), formatted with suffixes; report when none exist."""
        with self._lock:
            if not self.companies:
                return "No companies"
            lines = ["=== Company Value Rankings ==="]
            sorted_companies = sorted(
                self.companies.items(),
                key=lambda x: x[1].get('value', 0),
                reverse=True
            )[:10]
            
            for i, (_, data) in enumerate(sorted_companies, 1):
                name = data.get('name', 'Unknown')
                value = data.get('value', 0)
                lines.append(f"{i}. {name}: {fmt(value)}")
        
        return '\n'.join(lines)

    def _update_pause_flag(self) -> None:
        """Toggle pause/unpause via RCON when company presence changes, keeping internal paused flag in sync."""
        try:
            with self._lock:
                empty = not bool(self.companies)
                paused = self.paused
                
                if empty == paused:
                    return
                
                self.paused = empty
            
            cmd = "pause" if empty else "unpause"
            self.rcon(cmd)
            status = "Paused: no companies" if empty else "Unpaused: company present"
            self.log.info(status)
            
        except Exception as e:
            self.log.error(f"Failed to update pause state: {e}")

    def _reset_state(self) -> None:
        """Clear tracked state containers and flags to their defaults."""
        with self._lock:
            self.companies.clear()
            self.clients.clear()
            self.game_year = 0
            self.goal_reached = False
            self.reset_pending.clear()
            self.cooldowns.clear()
        self._reset_unnamed_company_one()

    def _refresh_after_load(self) -> None:
        """Re-poll state, clear unnamed company #1 if empty, and update pause flag."""
        self.poll_rcon()
        self._reset_unnamed_company_one()
        self._update_pause_flag()

    def _normalize_company_id(self, raw_id: int) -> int:
        """Convert OpenTTD company id (0-based, 255 spectator) to stored 1-based/spectator id."""
        return SPECTATOR_ID if raw_id == SPECTATOR_ID else raw_id + 1

    def _reset_unnamed_company_one(self) -> None:
        """Reset default unnamed company #1 if it exists and has no clients."""
        try:
            with self._lock:
                co = self.companies.get(1)
                has_clients = any(
                    c.get('company_id') == 1 
                    for c in self.clients.values()
                )
            
            if co and co.get("name") == "Unnamed" and not has_clients:
                self.rcon("reset_company 1")
                self.log.info("Reset unnamed company #1")
                
        except Exception as e:
            self.log.error(f"Failed to reset company #1: {e}")

    def auto_clean(self) -> None:
        """Identify companies older than clean_age and below clean_value, move their clients to spectators, then reset the companies."""
        try:
            clean_age = self.cfg.get('clean_age', 0)
            clean_value = self.cfg.get('clean_value', 0)
            
            if clean_age <= 0 or clean_value <= 0:
                return
            
            with self._lock:
                companies_to_clean: List[Tuple[int, Dict[str, Any], int]] = []
                for cid, data in list(self.companies.items()):
                    age = self.game_year - data.get('founded', self.game_year)
                    value = data.get('value', 0)
                    
                    if age >= clean_age and value < clean_value:
                        companies_to_clean.append((cid, data, age))
            
            for cid, data, age in companies_to_clean:
                try:
                    with self._lock:
                        clients_to_move = [
                            client_id 
                            for client_id, c in self.clients.items() 
                            if c.get('company_id') == cid
                        ]
                    
                    for client_id in clients_to_move:
                        self.rcon(f"move {client_id} {SPECTATOR_ID}")
                    
                    self.rcon(f"reset_company {cid}")
                    company_name = data.get('name', f'#{cid}')
                    self.msg(f"Company {company_name} auto-reset")
                    self.log.info(f"Auto-clean: co#{cid} {company_name} age={age} val={data.get('value', 0)}")
                    
                except Exception as e:
                    self.log.error(f"Failed to auto-clean company #{cid}: {e}")
                    
        except Exception as e:
            self.log.error(f"Error in auto_clean: {e}")

    def check_goal(self) -> None:
        """Detect first company meeting goal_value, announce winner with countdown, load scenario/new game, and fully reset state."""
        if self.goal_reached:
            return
        
        try:
            goal_value = self.cfg.get('goal_value', 0)
            if goal_value <= 0:
                return
            
            with self._lock:
                winner: Optional[Dict[str, Any]] = None
                for cid, data in list(self.companies.items()):
                    if data.get('value', 0) >= goal_value:
                        winner = data.copy()
                        self.goal_reached = True
                        break
            
            if not winner:
                return
            
            winner_name = winner.get('name', 'Unknown')
            winner_value = winner.get('value', 0)
            
            self.log.info(f"Goal reached: {winner_name} with {fmt(winner_value)}")
            self.msg(f"{winner_name} WINS! Reached {fmt(goal_value)} company value!")
            
            for countdown in [20, 15, 10, 5]:
                self.msg(f"Map resets in {countdown}s...")
                time.sleep(5)
            
            map_file = self.cfg.get('load_map', '')
            if not map_file:
                self.log.error("No load_map configured, cannot reset")
                return
            
            load_cmd = f"load_scenario {map_file}" if map_file.endswith('.scn') else f"load {map_file}"
            self.rcon(load_cmd)
            self._reset_state()
            self._refresh_after_load()

        except Exception as e:
            self.log.error(f"Error in check_goal: {e}")
            with self._lock:
                self.goal_reached = False

    def greet(self, client_id: int) -> None:
        """Wait briefly, then send a welcome message to a newly joined client."""
        time.sleep(GREETING_DELAY)
        
        if not self.running:
            return
        
        try:
            with self._lock:
                client_data = self.clients.get(client_id, {})
                name = client_data.get('name', f'Player{client_id}')
            
            self.log.info(f"Greet: {name} (#{client_id})")
            self.msg(f"Welcome {name}! Type !help for commands", client_id)
            
        except Exception as e:
            self.log.error(f"Error in greet: {e}")

    def handle_cmd(self, cid: int, text: str) -> None:
        """Parse and execute player commands with cooldown enforcement and error handling."""
        if self.paused:
            return
        
        now = time.time()
        with self._lock:
            last_cmd = self.cooldowns.get(cid, 0)
            if now - last_cmd < COMMAND_COOLDOWN:
                return
            self.cooldowns[cid] = now
        
        parts = text.split()
        if not parts:
            return
        
        cmd = parts[0].lower()
        self.log.info(f"Cmd: !{cmd} from #{cid}")
        
        try:
            if cmd == "help":
                self.msg("=== Commands ===\n!info !rules !cv !reset", cid)
                
            elif cmd == "info":
                goal_value = self.cfg.get('goal_value', 0)
                self.msg(
                    f"=== Game Info ===\n"
                    f"Goal: reach {fmt(goal_value)} company value\n"
                    f"Gamescript: Production Booster\n"
                    f"Primary Industries (Coal, Wood, Oil, Grain, etc)\n"
                    f"Transported >70% increases <50% decreases production",
                    cid
                )
                
            elif cmd == "rules":
                self.msg(
                    "=== Rules ===\n"
                    "1. Be respectful\n"
                    "2. No blocking competitors\n"
                    "3. No excessive pausing\n"
                    "4. Have fun!",
                    cid
                )
                
            elif cmd == "cv":
                self.msg(self.build_cv(), cid)
                
            elif cmd == "reset":
                with self._lock:
                    client_data = self.clients.get(cid, {})
                    co = client_data.get('company_id', SPECTATOR_ID)
                
                if co == SPECTATOR_ID or co not in self.companies:
                    self.msg("You must be in a company to reset it", cid)
                    return
                
                def expire_reset() -> None:
                    time.sleep(RESET_CONFIRM_TIMEOUT)
                    with self._lock:
                        if cid in self.reset_pending:
                            self.reset_pending.pop(cid)
                            self.log.debug(f"Reset expired for #{cid}")
                
                with self._lock:
                    self.reset_pending[cid] = co
                
                threading.Thread(target=expire_reset, daemon=True).start()
                self.msg(f"Move to spectator within {RESET_CONFIRM_TIMEOUT}s to reset company #{co}", cid)
                
        except Exception as e:
            self.log.error(f"Error handling command !{cmd}: {e}")

    def setup_handlers(self) -> None:
        """Register event handlers for all relevant OpenTTD admin packets."""
        if not self.admin:
            return
        
        @self.admin.add_handler(openttdpacket.ChatPacket)
        def on_chat(admin: Admin, pkt: openttdpacket.ChatPacket) -> None:
            """Log chat, and forward bang-prefixed messages to the command handler with the client id."""
            try:
                msg = pkt.message.strip()
                cid = getattr(pkt, 'id', None)
                self.log.debug(f"Chat received: cid={cid} msg='{msg}'")
                
                if msg.startswith('!') and cid is not None:
                    self.log.info(f"Command detected: {msg} from cid={cid}")
                    self.handle_cmd(cid, msg[1:])
                    
            except Exception as e:
                self.log.error(f"Error in on_chat handler: {e}", exc_info=True)

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        def on_client_info(admin: Admin, pkt: openttdpacket.ClientInfoPacket) -> None:
            """Record or update a client's name and company id (1-based, or 255 for spectators)."""
            try:
                co = self._normalize_company_id(pkt.company_id)
                with self._lock:
                    self.clients[pkt.id] = {
                        'name': pkt.name,
                        'company_id': co
                    }
                self.log.debug(f"ClientInfo: #{pkt.id} {pkt.name} co={co}")
                
            except Exception as e:
                self.log.error(f"Error in on_client_info: {e}")

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        def on_client_update(admin: Admin, pkt: openttdpacket.ClientUpdatePacket) -> None:
            """Track company changes for clients and, if a pending reset client moves to spectator, perform the reset."""
            try:
                co = self._normalize_company_id(pkt.company_id)
                
                with self._lock:
                    if pkt.id in self.clients:
                        self.clients[pkt.id]['company_id'] = co
                    
                    should_reset = False
                    pending_co = None
                    
                    if pkt.id in self.reset_pending and co == SPECTATOR_ID:
                        pending_co = self.reset_pending.pop(pkt.id)
                        should_reset = True
                
                if should_reset and pending_co is not None:
                    self.rcon(f"reset_company {pending_co}")
                    self.msg(f"Company #{pending_co} reset", pkt.id)
                    self.log.info(f"Reset confirmed: #{pkt.id} co={pending_co}")
                    
            except Exception as e:
                self.log.error(f"Error in on_client_update: {e}")

        @self.admin.add_handler(openttdpacket.ClientJoinPacket)
        def on_client_join(admin: Admin, pkt: openttdpacket.ClientJoinPacket) -> None:
            """Spawn a greeter thread for the newly joined client."""
            threading.Thread(target=self.greet, args=[pkt.id], daemon=True).start()

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        def on_client_remove(admin: Admin, pkt: Any) -> None:
            """Remove departing clients and clear any pending reset tied to them."""
            try:
                with self._lock:
                    self.clients.pop(pkt.id, None)
                    self.reset_pending.pop(pkt.id, None)
            except Exception as e:
                self.log.error(f"Error in on_client_remove: {e}")

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        def on_company_remove(admin: Admin, pkt: openttdpacket.CompanyRemovePacket) -> None:
            """Drop removed company from state and re-evaluate pause status."""
            try:
                cid = self._normalize_company_id(pkt.id)
                with self._lock:
                    self.companies.pop(cid, None)
                self._update_pause_flag()
            except Exception as e:
                self.log.error(f"Error in on_company_remove: {e}")

        @self.admin.add_handler(openttdpacket.CompanyInfoPacket, openttdpacket.CompanyNewPacket)
        def on_company_add(admin: Admin, pkt: Any) -> None:
            """Add or refresh a company entry on creation/info updates, then re-evaluate pause status."""
            try:
                cid = self._normalize_company_id(pkt.id)
                with self._lock:
                    if cid not in self.companies:
                        self.companies[cid] = {}
                self._update_pause_flag()
            except Exception as e:
                self.log.error(f"Error in on_company_add: {e}")

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        def on_new_game(admin: Admin, pkt: openttdpacket.NewGamePacket) -> None:
            """Clear all tracked state on new game, repoll, update pause flag, and log."""
            try:
                self._reset_state()
                self._refresh_after_load()
                self.log.info("New game detected, state reset")
            except Exception as e:
                self.log.error(f"Error in on_new_game: {e}")

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        def on_shutdown(admin: Admin, pkt: openttdpacket.ShutdownPacket) -> None:
            """Stop the run loop when the server signals shutdown."""
            self.running = False
            self.log.info("Server shutdown received")

    def run(self) -> None:
        """Connect/login, subscribe to updates, initialize handlers/state, then loop handling packets and scheduled tasks."""
        try:
            self.admin = Admin(
                ip=self.cfg['server_ip'],
                port=self.cfg['admin_port']
            )
            self.admin.login(
                self.cfg['admin_name'],
                self.cfg['admin_pass']
            )
            
            self.admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.CONSOLE, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
            
            self.setup_handlers()
            self._refresh_after_load()
            
            self.running = True
            self.log.info(f"Connected to {self.cfg['server_ip']}:{self.cfg['admin_port']}")
            self.msg("Admin connected")
            
        except Exception as e:
            self.log.error(f"Failed to initialize connection: {e}")
            raise

        next_tick = time.time() // POLL_INTERVAL * POLL_INTERVAL + POLL_INTERVAL
        now_dt = datetime.now()
        next_hourly = (now_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()

        while self.running:
            try:
                for pkt in self.admin.recv():
                    self.admin.handle_packet(pkt)
                
                if self.paused:
                    time.sleep(0.2)
                    continue
                
                now = time.time()
                
                if now >= next_tick:
                    if self.poll_rcon():
                        self._update_pause_flag()
                        self.auto_clean()
                        self.check_goal()
                    next_tick = time.time() // POLL_INTERVAL * POLL_INTERVAL + POLL_INTERVAL
                
                if now >= next_hourly:
                    self.msg(self.build_cv())
                    self.log.info("Hourly CV broadcast")
                    next_hourly += 3600
                    
            except Exception as e:
                self.log.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(1)

        self.log.info("Bot stopped")


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate configuration and return list of errors."""
    errors = []
    
    required_fields = [
        'server_ip', 'admin_name', 'admin_pass',
        'clean_age', 'clean_value', 'goal_value', 'load_map'
    ]
    
    for field in required_fields:
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
    """Continuously run a bot with automatic restart and backoff after errors."""
    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log.error(f"Configuration error: {error}")
        return
    
    while True:
        try:
            Bot(cfg, log).run()
            break
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            break
        except Exception as e:
            log.error(f"Error: {e}, reconnecting in {RECONNECT_DELAY}s...")
            time.sleep(RECONNECT_DELAY)


def main() -> None:
    """Entrypoint: set exit signals, load settings with validation, configure logging, spawn one bot per admin port, and block on threads."""
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
    
    if "admin_ports" not in settings or not settings["admin_ports"]:
        print("Error: admin_ports must be specified in settings.json")
        return
    
    logging.basicConfig(
        level=logging.DEBUG if settings.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    
    log = logging.getLogger("Bot")
    log.info("=== OpenTTD Admin Bot Starting ===")
    
    threads = []
    for port in settings["admin_ports"]:
        if not isinstance(port, int):
            log.error(f"Invalid port: {port}, skipping")
            continue
        
        cfg = {**settings, "admin_port": port}
        name = f"[{port}]"
        thread = threading.Thread(
            target=run_bot,
            args=(cfg, logging.getLogger(name)),
            daemon=True,
            name=name
        )
        thread.start()
        threads.append(thread)
    
    if not threads:
        log.error("No valid servers configured")
        return
    
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
