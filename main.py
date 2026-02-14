import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta

from pyopenttdadmin import Admin, openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.enums import Actions, ChatDestTypes

COMPANY_RE = re.compile(
    r"#\s*:?(\d+)(?:\([^)]*\))?\s+Company Name:\s*'([^']*)'\s+"
    r"Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+"
    r"Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I)
CLIENT_RE = re.compile(r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)", re.I)
DATE_RE = re.compile(r"Date:\s*(\d{4})-\d{2}-\d{2}")


def fmt(v):
    """Convert a number to a k/m/b suffixed string, choosing the largest matching threshold."""
    for threshold, suffix in [(1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")]:
        if v >= threshold:
            return f"{v/threshold:.1f}{suffix}"
    return str(v)


class Bot:
    def __init__(self, cfg, log):
        """Store shared config/logger and initialize runtime state containers and locks."""
        self.cfg, self.log = cfg, log
        self.admin = None
        self.running = False
        self._lock = threading.RLock()
        self.companies = {}
        self.clients = {}
        self.game_year = 0
        self.goal_reached = False
        self.cooldowns = {}
        self.reset_pending = {}
        self.paused = False

    def rcon(self, cmd):
        """Send an RCON command, collect line responses until the end packet or 5s timeout, and pass through other packets."""
        self.log.debug(f"RCON> {cmd}")
        self.admin.send_rcon(cmd)
        buf, done, deadline = [], False, time.time() + 5
        while not done and time.time() < deadline:
            for pkt in self.admin.recv():
                if isinstance(pkt, openttdpacket.RconPacket):
                    buf.append(pkt.response.strip())
                elif isinstance(pkt, openttdpacket.RconEndPacket):
                    done = True
                else:
                    self.admin.handle_packet(pkt)
        result = '\n'.join(buf)
        self.log.debug(f"RCON< {result[:200]}")
        return result

    def msg(self, text, cid=None):
        """Send each non-empty line as a chat message (broadcast or to a client) with a brief delay to avoid flooding."""
        for line in text.split('\n'):
            if line.strip():
                if cid:
                    self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT, cid)
                else:
                    self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.BROADCAST, 0)
                time.sleep(0.05)

    def poll_rcon(self):
        """Refresh companies, clients, and game year via RCON, parsing regex outputs into in-memory maps under lock."""
        try:
            co_out = self.rcon("companies")
            cl_out = self.rcon("clients")
            dt_out = self.rcon("get_date")
            
            with self._lock:
                self.companies.clear()
                for m in COMPANY_RE.finditer(co_out):
                    cid, name, year, money, loan, value = m.groups()
                    self.companies[int(cid)] = {'name': name.strip(), 'founded': int(year), 'value': int(value.replace(',', ''))}
                
                self.clients.clear()
                for m in CLIENT_RE.finditer(cl_out):
                    client_id, name, company_id = m.groups()
                    self.clients[int(client_id)] = {'name': name, 'company_id': int(company_id)}
                
                dm = DATE_RE.search(dt_out)
                if dm:
                    self.game_year = int(dm.group(1))
                    
            self.log.debug(f"Poll: {len(self.companies)} cos, {len(self.clients)} cls, year={self.game_year}")
        except Exception as e:
            self.log.error(f"Error in poll_rcon: {e}")

    def is_game_paused(self):
        """Query game date twice 3s apart; if unchanged, treat the game as paused, else running."""
        try:
            d1 = DATE_RE.search(self.rcon("get_date"))
            time.sleep(3)
            d2 = DATE_RE.search(self.rcon("get_date"))
            return bool(d1 and d2 and d1.group(0) == d2.group(0))
        except Exception:
            return False

    def build_cv(self):
        """Return a top-10 leaderboard of company values (descending), formatted with suffixes; report when none exist."""
        with self._lock:
            if not self.companies:
                return "No companies"
            lines = ["=== Company Value Rankings ==="]
            for i, (_, d) in enumerate(sorted(self.companies.items(), key=lambda x: x[1].get('value', 0), reverse=True)[:10], 1):
                lines.append(f"{i}. {d['name']}: {fmt(d.get('value', 0))}")
        return '\n'.join(lines)

    def _update_pause_flag(self):
        """Toggle pause/unpause via RCON when company presence changes, keeping internal paused flag in sync."""
        with self._lock:
            empty, paused = (not self.companies), self.paused
            if empty == paused:
                return
            self.paused = empty
        cmd = "pause" if empty else "unpause"
        self.rcon(cmd)
        self.log.info("Paused: no companies" if empty else "Unpaused: company present")

    def _reset_state(self):
        """Clear tracked state containers and flags to their defaults."""
        with self._lock:
            self.companies.clear()
            self.clients.clear()
            self.game_year = 0
            self.goal_reached = False
            self.reset_pending.clear()
        self._reset_unnamed_company_one()

    def _normalize_company_id(self, raw_id):
        """Convert OpenTTD company id (0-based, 255 spectator) to stored 1-based/spectator id."""
        return 255 if raw_id == 255 else raw_id + 1

    def _reset_unnamed_company_one(self):
        """Reset default unnamed company #1 once per startup/new game."""
        with self._lock:
            co = self.companies.get(1)
            has_clients = any(c.get('company_id') == 1 for c in self.clients.values())
        if co and co.get("name") == "Unnamed" and not has_clients:
            try:
                self.rcon("reset_company 1")
                self.log.info("Reset unnamed company #1")
            except Exception as e:
                self.log.error(f"Failed to reset company #1: {e}")

    def auto_clean(self):
        """Identify companies older than clean_age and below clean_value, move their clients to spectators, then reset the companies."""
        try:
            with self._lock:
                companies_to_clean = []
                for cid, data in list(self.companies.items()):
                    age = self.game_year - data.get('founded', self.game_year)
                    if age >= self.cfg['clean_age'] and data.get('value', 0) < self.cfg['clean_value']:
                        companies_to_clean.append((cid, data, age))
            
            for cid, data, age in companies_to_clean:
                with self._lock:
                    clients_to_move = [client_id for client_id, c in self.clients.items() if c['company_id'] == cid]
                
                for client_id in clients_to_move:
                    self.rcon(f"move {client_id} 255")
                
                self.rcon(f"reset_company {cid}")
                self.msg(f"Company {data['name']} auto-reset")
                self.log.info(f"Auto-clean: co#{cid} {data['name']} age={age} val={data.get('value',0)}")
        except Exception as e:
            self.log.error(f"Error in auto_clean: {e}")

    def check_goal(self):
        """Detect first company meeting goal_value, announce winner with countdown, load scenario/new game, and fully reset state."""
        if self.goal_reached:
            return
        
        try:
            with self._lock:
                winner = None
                for cid, data in list(self.companies.items()):
                    if data.get('value', 0) >= self.cfg['goal_value']:
                        winner = data
                        self.goal_reached = True
                        break
            
            if winner:
                self.log.info(f"Goal reached: {winner['name']} with {fmt(winner['value'])}")
                self.msg(f"{winner['name']} WINS! Reached {fmt(self.cfg['goal_value'])} company value!")
                prev = 20
                for countdown in [20, 15, 10, 5]:
                    self.msg(f"Map resets in {countdown}s...")
                    time.sleep(prev - countdown if prev > countdown else 5)
                    prev = countdown
                time.sleep(5)
                map_file = self.cfg['load_map']
                try:
                    load_cmd = f"load_scenario {map_file}" if map_file.endswith('.scn') else f"load {map_file}"
                    res = self.rcon(load_cmd)
                    if res and any(word in res.lower() for word in ["cannot be found"]):
                        self.log.warning(f"Load command reported failure, starting new game. Response: {res}")
                        self.rcon("newgame")
                except Exception as load_err:
                    self.log.error(f"Load command crashed, starting new game: {load_err}")
                    self.rcon("newgame")
                
                self._reset_state()
                self.poll_rcon()
                self._update_pause_flag()
                self.log.info("New game detected, state reset")
        except Exception as e:
            self.log.error(f"Error in check_goal: {e}")

    def greet(self, client_id):
        """After a short delay, greet a joining client by name (or fallback), if the bot is still running."""
        time.sleep(5)
        if not self.running:
            return
        try:
            with self._lock:
                name = self.clients.get(client_id, {}).get('name', f'Player{client_id}')
            self.log.info(f"Greet: {name} (#{client_id})")
            self.msg(f"Welcome {name}! Type !help for commands", client_id)
        except Exception as e:
            self.log.error(f"Error in greet: {e}")

    def handle_cmd(self, cid, text):
        """Process whitelisted commands with per-client cooldown; serve info/rules/leaderboard or manage self-initiated company reset flow."""
        if self.paused:
            return
        
        cmd = text.split()[0].lower() if text.split() else ""
        if cmd not in {"help", "info", "rules", "cv", "reset"}:
            return

        now = time.time()
        with self._lock:
            if now - self.cooldowns.get(cid, 0) < 3:
                return
            self.cooldowns[cid] = now

        self.log.info(f"Cmd: !{cmd} from #{cid}")
        
        try:
            if cmd == "help":
                self.msg("=== Commands ===\n!info !rules !cv !reset", cid)
            elif cmd == "info":
                self.msg(f"=== Game Info ===\nGoal: reach {fmt(self.cfg['goal_value'])} company value\nGamescript: Production Booster\nPrimary Industries(Coal, Wood, Oil, Grain, etc)\nTransported >70% increases <50% decreases production", cid)
            elif cmd == "rules":
                self.msg(f"=== Server Rules ===\n1. No griefing/sabotage\n2. No blocking players\n3. No cheating/exploits\n4. Be respectful\n5. Inactive companies (more than {self.cfg['clean_age']}years & company value less than {fmt(self.cfg['clean_value'])}) will auto-reset", cid)
            elif cmd == "cv":
                self.poll_rcon()
                self.msg(self.build_cv(), cid)
            elif cmd == "reset":
                with self._lock:
                    client = self.clients.get(cid)
                    if not client or client.get('company_id') == 255:
                        self.msg("Must be in a company to reset", cid)
                        return
                    co = client['company_id']
                    if cid in self.reset_pending:
                        self.msg("Reset already pending. Move to spectator to confirm", cid)
                        return

                def expire():
                    """Cancel a pending reset if the client fails to switch to spectator within the timeout."""
                    time.sleep(10)
                    with self._lock:
                        if cid in self.reset_pending:
                            self.reset_pending.pop(cid, None)
                            self.msg("Reset cancelled (timeout)", cid)

                with self._lock:
                    self.reset_pending[cid] = co
                threading.Thread(target=expire, daemon=True).start()
                self.msg(f"Move to spectator within 10s to reset company #{co}", cid)
        except Exception as e:
            self.log.error(f"Error handling command: {e}")

    def setup_handlers(self):
        """Register OpenTTD packet handlers that maintain state, react to chat/joins/leaves, and enforce pause/reset logic."""
        @self.admin.add_handler(openttdpacket.ChatPacket)
        def on_chat(admin, pkt):
            """Log chat, and forward bang-prefixed messages to the command handler with the client id."""
            try:
                msg = pkt.message.strip()
                cid = getattr(pkt, 'id', None)
                self.log.debug(f"Chat received: cid={cid} msg='{msg}'")
                if msg.startswith('!'):
                    self.log.info(f"Command detected: {msg} from cid={cid}")
                    self.handle_cmd(cid, msg[1:])
            except Exception as e:
                self.log.error(f"Error in on_chat handler: {e}", exc_info=True)

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        def on_client_info(admin, pkt):
            """Record or update a client's name and company id (1-based, or 255 for spectators)."""
            try:
                co = self._normalize_company_id(pkt.company_id)
                with self._lock:
                    self.clients[pkt.id] = {'name': pkt.name, 'company_id': co}
                self.log.debug(f"ClientInfo: #{pkt.id} {pkt.name} co={co}")
            except Exception as e:
                self.log.error(f"Error in on_client_info: {e}")

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        def on_client_update(admin, pkt):
            """Track company changes for clients and, if a pending reset client moves to spectator, perform the reset."""
            try:
                co = self._normalize_company_id(pkt.company_id)
                with self._lock:
                    if pkt.id in self.clients:
                        self.clients[pkt.id]['company_id'] = co
                    
                    if pkt.id in self.reset_pending and co == 255:
                        pending_co = self.reset_pending.pop(pkt.id)
                        should_reset = True
                    else:
                        should_reset = False
                        pending_co = None
                
                if should_reset:
                    self.rcon(f"reset_company {pending_co}")
                    self.msg(f"Company #{pending_co} reset", pkt.id)
                    self.log.info(f"Reset confirmed: #{pkt.id} co={pending_co}")
            except Exception as e:
                self.log.error(f"Error in on_client_update: {e}")

        @self.admin.add_handler(openttdpacket.ClientJoinPacket)
        def on_client_join(admin, pkt):
            """Spawn a greeter thread for the newly joined client."""
            threading.Thread(target=self.greet, args=[pkt.id], daemon=True).start()

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        def on_client_remove(admin, pkt):
            """Remove departing clients and clear any pending reset tied to them."""
            with self._lock:
                self.clients.pop(pkt.id, None)
                self.reset_pending.pop(pkt.id, None)

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        def on_company_remove(admin, pkt):
            """Drop removed company from state and re-evaluate pause status."""
            cid = self._normalize_company_id(pkt.id)
            with self._lock:
                self.companies.pop(cid, None)
            self._update_pause_flag()

        @self.admin.add_handler(openttdpacket.CompanyInfoPacket, openttdpacket.CompanyNewPacket)
        def on_company_add(admin, pkt):
            """Add or refresh a company entry on creation/info updates, then re-evaluate pause status."""
            with self._lock:
                self.companies[self._normalize_company_id(pkt.id)] = {}
            self._update_pause_flag()

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        def on_new_game(admin, pkt):
            """Clear all tracked state on new game, repoll, update pause flag, and log."""
            self._reset_state()
            self.poll_rcon()
            self._reset_unnamed_company_one()
            self._update_pause_flag()
            self.log.info("New game detected, state reset")

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        def on_shutdown(admin, pkt):
            """Stop the run loop when the server signals shutdown."""
            self.running = False
            self.log.info("Server shutdown received")


    def run(self):
        """Connect/login, subscribe to updates, initialize handlers/state, then loop handling packets and scheduled tasks (poll, clean, goal check, hourly CV)."""
        self.admin = Admin(ip=self.cfg['server_ip'], port=self.cfg['admin_port'])
        self.admin.login(self.cfg['admin_name'], self.cfg['admin_pass'])
        self.admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
        self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        self.admin.subscribe(AdminUpdateType.CONSOLE, AdminUpdateFrequency.AUTOMATIC)
        self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
        self.setup_handlers()
        self.poll_rcon()
        self._reset_unnamed_company_one()
        self.paused = self.is_game_paused()
        self._update_pause_flag()
        self.running = True
        self.log.info(f"Connected to {self.cfg['server_ip']}:{self.cfg['admin_port']}")
        self.msg("Admin connected")

        next_tick = time.time() // 60 * 60 + 60
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
                    self.poll_rcon()
                    self._update_pause_flag()
                    self.auto_clean()
                    self.check_goal()
                    next_tick = time.time() // 60 * 60 + 60
                if now >= next_hourly:
                    self.msg(self.build_cv())
                    self.log.info("Hourly CV broadcast")
                    next_hourly += 3600
            except Exception as e:
                self.log.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(1)

        self.log.info("Bot stopped")


def run_bot(cfg, log):
    """Continuously run a bot with automatic restart and 10s backoff after errors."""
    while True:
        try:
            Bot(cfg, log).run()
            break
        except Exception as e:
            log.error(f"Error: {e}, reconnecting in 10s...")
            time.sleep(10)


def main():
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
    
    logging.basicConfig(
        level=logging.DEBUG if settings.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("Bot")
    log.info("=== OpenTTD Admin Bot Starting ===")
    threads = []
    for port in settings["admin_ports"]:
        cfg = {**settings, "admin_port": port}
        name = f"[{port}]"
        t = threading.Thread(target=run_bot, args=(cfg, logging.getLogger(name)), daemon=True, name=name)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
