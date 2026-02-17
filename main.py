import asyncio
import configparser
import logging
import os
import re
import signal
from datetime import date, timedelta
from typing import Dict, Optional, Any, List, Set

from aiopyopenttdadmin import Admin, AdminUpdateType, AdminUpdateFrequency, openttdpacket
from pyopenttdadmin.enums import Actions, ChatDestTypes

COMPANY_RE = re.compile(r"#:\s*(\d+)(?:\([^)]*\))?\s+Company Name:\s*'([^']*)'\s+Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I)

SPECTATOR_ID = 255
MAX_COMPANIES_PER_IP = 2
VIOLATION_THRESHOLD = 3
VIOLATION_WINDOW = 60
BROADCAST_INTERVAL = 3600
RCON_TIMEOUT = 5
MAIN_LOOP_INTERVAL = 60
GREETING_DELAY = 5
CHAT_COMMAND_COOLDOWN = 2
RESET_CONFIRM_TIMEOUT = 15
COOLDOWN_CLEANUP_INTERVAL = 300
RECONNECT_DELAY = 15

def fmt(v: int) -> str:
    """Format integer with abbreviated suffixes (b/m/k)."""
    for t, s in [(1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")]:
        if v >= t: 
            return f"{v/t:.1f}{s}"
    return str(v)

def parse_int(s: str) -> int:
    """Parse integer from string, removing commas."""
    return int(s.replace(',', ''))

def load_config(path: str = "settings.cfg") -> List[Dict[str, Any]]:
    """Load server configurations from INI file."""
    config = configparser.ConfigParser()
    if not config.read(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    
    servers = []
    for section in config.sections():
        cfg = {}
        for key, value in config.items(section):
            if value.isdigit():
                cfg[key] = int(value)
            elif value.lower() in ('true', 'false'):
                cfg[key] = value.lower() == 'true'
            else:
                cfg[key] = value
        servers.append(cfg)
    
    return servers

class Bot:
    def __init__(self, cfg: Dict[str, Any], log: logging.Logger):
        """Initialize bot with config and logger."""
        self.cfg, self.log = cfg, log
        self.admin: Optional[Admin] = None
        self.running = False
        self._lock = asyncio.Lock()
        self.companies: Dict[int, Dict[str, Any]] = {}
        self.company_owners: Dict[int, str] = {}
        self.clients: Dict[int, Dict[str, Any]] = {}
        self.game_year = 0
        self.game_date: Optional[int] = None
        self.is_paused = True
        self.goal_reached = False
        self.cooldowns: Dict[int, float] = {}
        self.reset_pending: Dict[int, tuple[int, float]] = {}
        self.last_pause_cmd: Optional[bool] = None
        self.last_cmd_time = 0.0
        self.tasks: Set[asyncio.Task] = set()
        self.connection_warned = False
        self.violations: Dict[str, List[float]] = {}

    async def rcon(self, cmd: str, timeout: float = RCON_TIMEOUT) -> str:
        """Execute RCON command and return response."""
        if not self.admin: 
            raise RuntimeError("Admin connection not initialized")
        
        self.log.debug(f"RCON> {cmd}")
        
        try:
            await self.admin.send_rcon(cmd)
        except Exception as e:
            self.log.error(f"Failed to send RCON command '{cmd}': {e}")
            raise
        
        buf = []
        done = False
        deadline = asyncio.get_event_loop().time() + timeout
        
        while not done:
            loop_time = asyncio.get_event_loop().time()
            if loop_time >= deadline:
                raise TimeoutError(f"RCON command '{cmd}' timed out after {timeout}s")
            
            try:
                remaining = deadline - loop_time
                packets = await asyncio.wait_for(self.admin.recv(), timeout=remaining)
                
                for pkt in packets:
                    if isinstance(pkt, openttdpacket.RconPacket): 
                        buf.append(pkt.response.strip())
                    elif isinstance(pkt, openttdpacket.RconEndPacket): 
                        done = True
                        break
                    else: 
                        await self.admin.handle_packet(pkt)
                        
            except asyncio.TimeoutError:
                raise TimeoutError(f"RCON command '{cmd}' timed out after {timeout}s")
        
        result = '\n'.join(buf)
        self.log.debug(f"RCON< {result[:200]}")
        return result

    async def msg(self, text: str, cid: Optional[int] = None) -> None:
        """Send message to client or broadcast."""
        if not self.admin: 
            return
        
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            try:
                if cid is not None:
                    await self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT, cid)
                else:
                    await self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.BROADCAST, 0)
                # No delay needed - _chat() is async and waits for server to process each message
            except Exception as e: 
                self.log.error(f"Failed to send message '{line[:50]}...': {e}")

    async def poll_state(self) -> bool:
        """Poll server state via RCON."""
        try:
            co_out = await self.rcon("companies")
            
            async with self._lock:
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
            
            self.log.info(f"Poll: {len(self.companies)} companies, {len(self.clients)} clients, year {self.game_year}")
            return True
            
        except Exception as e:
            self.log.error(f"Poll error: {e}")
            return False

    async def poll_clients(self) -> bool:
        """Poll client list via RCON."""
        try:
            cl_out = await self.rcon("clients")
            
            async with self._lock:
                for line in cl_out.split('\n'):
                    # Format: "Client #4  name: 'gruntwork'  company: 1  IP: 127.0.0.1"
                    # Or spectator: "Client #4  name: 'gruntwork'  company: 255  IP: 127.0.0.1"
                    match = re.search(r'Client #(\d+)\s+name:\s*\'([^\']+)\'\s+company:\s*(\d+)\s+IP:\s*([\d.]+)', line)
                    if match:
                        cid, name, co_raw, ip = match.groups()
                        co = int(co_raw)
                        self.clients[int(cid)] = {
                            'name': name,
                            'company_id': SPECTATOR_ID if co == 255 else co,
                            'ip': ip
                        }
            
            self.log.debug(f"Polled {len(self.clients)} existing clients")
            return True
            
        except Exception as e:
            self.log.error(f"Client poll error: {e}")
            return False

    def build_cv(self) -> str:
        """Build company value leaderboard."""
        if not self.companies: 
            return "No companies"
        
        sorted_cos = sorted(self.companies.items(), key=lambda x: x[1].get('value', 0), reverse=True)[:10]
        lines = ["=== Company Value Rankings ==="]
        lines.extend(f"{i}. {d['name']}: {fmt(d['value'])}" for i, (_, d) in enumerate(sorted_cos, 1))
        return '\n'.join(lines)

    def normalize_company_id(self, raw_id: int) -> int:
        """Convert packet company ID (0-based, 255=spectator) to internal ID (1-based, 255=spectator)."""
        return SPECTATOR_ID if raw_id == SPECTATOR_ID else raw_id + 1

    def count_companies_by_ip(self, ip: str) -> int:
        """Count how many companies are owned by the given IP."""
        return sum(1 for owner_ip in self.company_owners.values() if owner_ip == ip)

    async def apply_pause_policy(self) -> None:
        """Apply pause/unpause based on company count."""
        async with self._lock:
            should_pause = len(self.companies) == 0
            now = asyncio.get_event_loop().time()
            
            if self.last_pause_cmd == should_pause:
                return
            
            # Rate limit unpause only (pause is critical, never rate-limited)
            if not should_pause and now - self.last_cmd_time < 1.0:
                return
            
            self.last_cmd_time = now
        
        try:
            cmd = "pause" if should_pause else "unpause"
            response = await self.rcon(cmd)
            if f"already {'paused' if should_pause else 'unpaused'}" not in response.lower():
                self.log.info(f"{'Paused' if should_pause else 'Unpaused'}: {'no companies' if should_pause else 'company present'}")
            async with self._lock:
                self.last_pause_cmd = should_pause
                self.is_paused = should_pause
        except Exception as e:
            self.log.error(f"Pause policy error: {e}")

    async def reset_unnamed_co1(self) -> None:
        """Reset company #1 if unnamed and no clients."""
        try:
            async with self._lock:
                co1 = self.companies.get(1)
                if not co1 or co1.get('name') != 'Unnamed':
                    return
                if any(c['company_id'] == 1 for c in self.clients.values()):
                    return
            
            await self.rcon("reset_company 1")
            self.log.info("Reset unnamed company #1")
        except Exception as e:
            self.log.error(f"Error resetting unnamed co1: {e}")

    async def auto_clean(self) -> None:
        """Auto-reset old low-value companies."""
        age_thresh = self.cfg.get('clean_age', 0)
        val_thresh = self.cfg.get('clean_value', 0)
        if age_thresh <= 0 or val_thresh <= 0: 
            return
        
        async with self._lock:
            to_clean = []
            for cid, d in self.companies.items():
                age = self.game_year - d.get('founded', self.game_year)
                if age >= age_thresh and d.get('value', 0) < val_thresh:
                    clients = [c for c, cd in self.clients.items() if cd['company_id'] == cid]
                    to_clean.append((cid, d['name'], age, d['value'], clients))
        
        for cid, name, age, value, clients in to_clean:
            try:
                for c in clients:
                    await self.rcon(f"move {c} {SPECTATOR_ID}")
                await self.rcon(f"reset_company {cid}")
                await self.msg(f"Company {name} auto-reset")
                self.log.info(f"Auto-clean: co#{cid} {name} age={age} val={value}")
            except Exception as e:
                self.log.error(f"Error auto-cleaning company #{cid}: {e}")

    async def reset_state(self) -> None:
        """Reset all tracked state."""
        async with self._lock:
            self.companies.clear()
            self.company_owners.clear()
            self.clients.clear()
            self.game_year = 0
            self.game_date = None
            self.is_paused = True
            self.goal_reached = False
            self.reset_pending.clear()
            self.cooldowns.clear()
            self.violations.clear()
            self.last_pause_cmd = None
            self.last_cmd_time = 0.0

    async def check_goal(self) -> None:
        """Check goal and trigger map reload."""
        goal = self.cfg.get('goal', 0)
        if self.goal_reached or goal <= 0: 
            return
        
        async with self._lock:
            winner = next((d for d in self.companies.values() if d.get('value', 0) >= goal), None)
            if winner:
                self.goal_reached = True
                winner_name = winner['name']
                winner_value = winner['value']
        
        if not winner: 
            return
        
        self.log.info(f"Goal: {winner_name} reached {fmt(winner_value)}")
        await self.msg(f"{winner_name} WINS! Reached {fmt(goal)}!")
        
        for t in [20, 15, 10, 5]:
            await self.msg(f"Map reloads in {t}s...")
            await asyncio.sleep(5)
        
        try:
            if map_file := self.cfg.get('map', ''):
                cmd = f"load_scenario {map_file}" if map_file.endswith('.scn') else f"load {map_file}"
                await self.rcon(cmd)
                await asyncio.sleep(1)
                await self.reset_state()
                await self.poll_clients()
                await self.poll_state()
                await self.reset_unnamed_co1()
                await self.apply_pause_policy()
        except Exception as e:
            self.log.error(f"Error reloading map: {e}")

    async def greet(self, cid: int) -> None:
        """Greet new client with pause-aware message."""
        await asyncio.sleep(GREETING_DELAY)
        if not self.running: 
            return
        
        try:
            async with self._lock:
                name = self.clients.get(cid, {}).get('name', f'Player{cid}')
                paused = self.is_paused
            
            self.log.info(f"Greeting: {name} (#{cid}, paused={paused})")
            msg_text = f"Welcome {name}, create a company to unpause game, type !help for commands" if paused else f"Welcome {name}, type !help for commands"
            await self.msg(msg_text, cid)
            
        except Exception as e:
            self.log.error(f"Error greeting client #{cid}: {e}")

    def create_task(self, coro) -> asyncio.Task:
        """Create and track an async task."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def handle_cmd(self, cid: int, text: str) -> None:
        """Process chat commands."""
        loop = asyncio.get_event_loop()
        
        async with self._lock:
            if self.is_paused:
                return
            now = loop.time()
            if now - self.cooldowns.get(cid, 0) < CHAT_COMMAND_COOLDOWN: 
                return
            self.cooldowns[cid] = now
        
        parts = text.split()
        if not parts: 
            return
        
        cmd = parts[0].lower()
        self.log.info(f"Command: !{cmd} from #{cid}")
        
        try:
            if cmd == "help":
                await self.msg("=== Commands ===\n!info !rules !cv !reset", cid)
            
            elif cmd == "info":
                await self.msg(f"Goal: first company to reach {fmt(self.cfg.get('goal', 0))} company value wins!\nGamescript: Production Booster v3\nPrimary industries(coal,wood,oil,etc)\nTransported >70% increases production, <50% decreases", cid)
            
            elif cmd == "rules":
                await self.msg(f"1. No sabotage, respect other players\n2. No griefing or blocking industries/cities\n3. Do not excessively reserve land\n4. Companies >{self.cfg.get('clean_age', 2)}yrs & company value <{fmt(self.cfg.get('clean_value', 1000))} auto-cleaned\n5. Only {MAX_COMPANIES_PER_IP} companies allowed per client", cid)
            
            elif cmd == "cv":
                await self.msg(self.build_cv(), cid)
            
            elif cmd == "reset":
                async with self._lock:
                    co = self.clients.get(cid, {}).get('company_id', SPECTATOR_ID)
                    if co == SPECTATOR_ID or co not in self.companies:
                        await self.msg("You must be in a company", cid)
                        return
                    token = loop.time()
                    self.reset_pending[cid] = (co, token)
                
                self.log.info(f"Reset: client #{cid} company #{co}")
                await self.msg(f"Move to spectator in {RESET_CONFIRM_TIMEOUT}s to reset company", cid)
                
                async def timeout_handler(request_token: float) -> None:
                    await asyncio.sleep(RESET_CONFIRM_TIMEOUT)
                    async with self._lock:
                        pending = self.reset_pending.get(cid)
                        if pending and pending[1] == request_token:
                            self.reset_pending.pop(cid, None)
                            self.log.debug(f"Reset timeout: client #{cid}")
                    
                    if pending and pending[1] == request_token:
                        await self.msg(f"Reset timeout after {RESET_CONFIRM_TIMEOUT}s", cid)
                
                self.create_task(timeout_handler(token))
                
        except Exception as e:
            self.log.error(f"Error handling command !{cmd}: {e}")

    def setup_handlers(self) -> None:
        """Register packet handlers."""
        if not self.admin: 
            return

        @self.admin.add_handler(openttdpacket.ConsolePacket)
        async def on_console(admin: Admin, pkt: openttdpacket.ConsolePacket) -> None:
            try:
                msg = pkt.message.strip().lower()
                if "game paused" in msg or "paused" in msg and "game" in msg:
                    async with self._lock:
                        self.is_paused = True
                    self.log.debug(f"Console: Game paused")
                elif "game unpaused" in msg or "unpaused" in msg:
                    async with self._lock:
                        self.is_paused = False
                    self.log.debug(f"Console: Game unpaused")
            except Exception as e:
                self.log.error(f"Error in on_console: {e}")

        @self.admin.add_handler(openttdpacket.ChatPacket)
        async def on_chat(admin: Admin, pkt: openttdpacket.ChatPacket) -> None:
            try:
                msg = pkt.message.strip()
                cid = getattr(pkt, 'id', None)
                
                if msg.startswith('!') and cid is not None:
                    self.log.debug(f"Command received: {msg} from #{cid}")
                    await self.handle_cmd(cid, msg[1:])
            except Exception as e:
                self.log.error(f"Error in on_chat handler: {e}", exc_info=True)

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        async def on_client_info(admin: Admin, pkt: openttdpacket.ClientInfoPacket) -> None:
            try:
                co = self.normalize_company_id(pkt.company_id)
                ip = getattr(pkt, 'ip', '0.0.0.0')
                async with self._lock:
                    self.clients[pkt.id] = {'name': pkt.name, 'company_id': co, 'ip': ip}
                self.log.debug(f"ClientInfo: #{pkt.id} {pkt.name} co={co} ip={ip}")
            except Exception as e:
                self.log.error(f"Error in on_client_info: {e}")

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        async def on_client_update(admin: Admin, pkt: openttdpacket.ClientUpdatePacket) -> None:
            try:
                co = self.normalize_company_id(pkt.company_id)
                
                async with self._lock:
                    old_co = None
                    if pkt.id in self.clients:
                        old_co = self.clients[pkt.id].get('company_id')
                        self.clients[pkt.id]['company_id'] = co
                    else:
                        ip = getattr(pkt, 'ip', '0.0.0.0')
                        self.clients[pkt.id] = {'name': pkt.name, 'company_id': co, 'ip': ip}
                    
                    # Check if client just joined a company (not spectator, and changed from spectator)
                    if co != SPECTATOR_ID and old_co == SPECTATOR_ID:
                        client_ip = self.clients[pkt.id].get('ip')
                        if client_ip:
                            # Record ownership if this is a new company
                            if co not in self.company_owners:
                                self.company_owners[co] = client_ip
                            company_count = self.count_companies_by_ip(client_ip)
                            
                            if company_count > MAX_COMPANIES_PER_IP:
                                self.log.warning(f"IP {client_ip} exceeded limit ({company_count}/{MAX_COMPANIES_PER_IP}), removing company #{co}")
                                # Schedule enforcement (can't await in lock)
                                enforce_limit = True
                            else:
                                enforce_limit = False
                        else:
                            enforce_limit = False
                    else:
                        enforce_limit = False
                    
                    pending_co = None
                    if co == SPECTATOR_ID:
                        pending = self.reset_pending.pop(pkt.id, None)
                        if pending:
                            pending_co = pending[0]
                
                # Enforce IP limit outside lock
                if enforce_limit:
                    try:
                        await asyncio.sleep(0.3)
                        
                        # Record violation and check for abuse
                        now = asyncio.get_event_loop().time()
                        async with self._lock:
                            client_ip = self.clients[pkt.id].get('ip')
                            if client_ip not in self.violations:
                                self.violations[client_ip] = []
                            self.violations[client_ip].append(now)
                            
                            # Remove old violations outside window
                            self.violations[client_ip] = [t for t in self.violations[client_ip] if now - t <= VIOLATION_WINDOW]
                            
                            # Check if this is abuse (3+ violations in 60s)
                            is_abuse = len(self.violations[client_ip]) >= VIOLATION_THRESHOLD
                        
                        if is_abuse:
                            # Kick abusive client
                            await self.msg(f"Kicked: Repeated abuse ({VIOLATION_THRESHOLD} violations in {VIOLATION_WINDOW}s)", pkt.id)
                            await self.rcon(f"move {pkt.id} {SPECTATOR_ID}")
                            await self.rcon(f"reset_company {co}")
                            await self.rcon(f"kick {pkt.id}")
                            self.log.warning(f"Kicked client #{pkt.id} (IP {client_ip}) for abuse: {len(self.violations[client_ip])} violations")
                            async with self._lock:
                                self.violations.pop(client_ip, None)
                                self.companies.pop(co, None)
                                self.company_owners.pop(co, None)
                        else:
                            # Normal enforcement
                            await self.msg(f"Only {MAX_COMPANIES_PER_IP} companies per client allowed.", pkt.id)
                            await self.rcon(f"move {pkt.id} {SPECTATOR_ID}")
                            await self.rcon(f"reset_company {co}")
                            async with self._lock:
                                self.companies.pop(co, None)
                                self.company_owners.pop(co, None)
                                if pkt.id in self.clients:
                                    self.clients[pkt.id]['company_id'] = SPECTATOR_ID
                        return
                    except Exception as e:
                        self.log.error(f"Error enforcing IP limit: {e}")
                
                # Handle !reset confirmation
                if pending_co:
                    await self.rcon(f"reset_company {pending_co}")
                    await self.msg(f"Company #{pending_co} reset", pkt.id)
                    async with self._lock:
                        self.companies.pop(pending_co, None)
                        self.company_owners.pop(pending_co, None)
                        self.last_pause_cmd = None
                    self.log.info(f"Reset complete: company #{pending_co}")
                    await self.poll_state()
                    await self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_client_update: {e}")

        @self.admin.add_handler(openttdpacket.ClientJoinPacket)
        async def on_client_join(admin: Admin, pkt: openttdpacket.ClientJoinPacket) -> None:
            self.create_task(self.greet(pkt.id))

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        async def on_client_remove(admin: Admin, pkt: Any) -> None:
            try:
                async with self._lock:
                    self.clients.pop(pkt.id, None)
                    self.reset_pending.pop(pkt.id, None)
            except Exception as e:
                self.log.error(f"Error in on_client_remove: {e}")

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin: Admin, pkt: openttdpacket.CompanyRemovePacket) -> None:
            try:
                cid = self.normalize_company_id(pkt.id)
                async with self._lock:
                    if self.companies.pop(cid, None):
                        self.log.info(f"Company removed: #{cid}")
                    self.company_owners.pop(cid, None)
                await self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_company_remove: {e}")

        @self.admin.add_handler(openttdpacket.CompanyInfoPacket, openttdpacket.CompanyNewPacket)
        async def on_company_add(admin: Admin, pkt: Any) -> None:
            try:
                cid = self.normalize_company_id(pkt.id)
                async with self._lock:
                    added = cid not in self.companies
                    if added:
                        self.companies[cid] = {}
                if added:
                    self.log.info(f"Company added: #{cid}")
                    await self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_company_add: {e}")

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        async def on_new_game(admin: Admin, pkt: openttdpacket.NewGamePacket) -> None:
            try:
                self.log.info("New game detected")
                await self.reset_state()
                await self.poll_clients()
                await self.poll_state()
                await self.reset_unnamed_co1()
                await self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Error in on_new_game: {e}")

        @self.admin.add_handler(openttdpacket.DatePacket)
        async def on_date(admin: Admin, pkt: openttdpacket.DatePacket) -> None:
            try:
                async with self._lock:
                    self.game_year = (date(1, 1, 1) + timedelta(days=pkt.date)).year - 1
                    self.game_date = pkt.date
                self.log.debug(f"Date: {pkt.date} (year {self.game_year})")
            except Exception as e:
                self.log.error(f"Error in on_date: {e}")

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        async def on_shutdown(admin: Admin, pkt: openttdpacket.ShutdownPacket) -> None:
            try:
                self.running = False
                self.log.info("Server shutdown")
            except Exception as e:
                self.log.error(f"Error in on_shutdown: {e}")

    async def cleanup(self) -> None:
        """Cleanup resources and cancel tasks."""
        self.log.info("Cleaning up bot resources...")
        
        for task in list(self.tasks):
            if not task.done():
                task.cancel()
        
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        
        if self.admin and self.admin._writer:
            try:
                if not self.admin._writer.is_closing():
                    self.admin._writer.close()
                await self.admin._writer.wait_closed()
            except Exception as e:
                self.log.error(f"Error closing admin connection: {e}")
        
        self.log.info("Cleanup complete")

    async def run(self) -> None:
        """Main bot loop."""
        try:
            self.admin = Admin(ip=self.cfg['ip'], port=self.cfg['port'])
            await self.admin.login(self.cfg['admin_name'], self.cfg['admin_pass'])
            await self.admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
            await self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
            await self.admin.subscribe(AdminUpdateType.CONSOLE, AdminUpdateFrequency.AUTOMATIC)
            await self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
            await self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.DAILY)
            
            self.setup_handlers()
            await self.poll_clients()
            await self.poll_state()
            await self.reset_unnamed_co1()
            await self.apply_pause_policy()
            self.running = True
            
            self.log.info(f"Connected to {self.cfg['ip']}:{self.cfg['port']}")
            await self.msg("Admin connected")
            
        except Exception as e:
            self.log.error(f"Failed to initialize connection: {e}")
            raise
        
        loop = asyncio.get_event_loop()
        next_tick = loop.time() + MAIN_LOOP_INTERVAL
        next_broadcast = loop.time() + BROADCAST_INTERVAL
        next_cleanup = loop.time() + COOLDOWN_CLEANUP_INTERVAL
        
        while self.running:
            try:
                try:
                    packets = await self.admin.recv()
                    
                    if packets:
                        for pkt in packets:
                            await self.admin.handle_packet(pkt)
                        async with self._lock:
                            self.connection_warned = False
                    
                except Exception as e:
                    async with self._lock:
                        if not self.connection_warned:
                            self.log.warning(f"Connection issue: {e}")
                            self.connection_warned = True
                    await asyncio.sleep(1)
                    continue
                
                now = loop.time()
                
                async with self._lock:
                    paused = self.is_paused
                    should_cleanup = now >= next_cleanup
                    if should_cleanup:
                        self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < COOLDOWN_CLEANUP_INTERVAL}
                        # Clean up old violations
                        for ip in list(self.violations.keys()):
                            self.violations[ip] = [t for t in self.violations[ip] if now - t <= VIOLATION_WINDOW]
                            if not self.violations[ip]:
                                self.violations.pop(ip)
                
                if should_cleanup:
                    next_cleanup = now + COOLDOWN_CLEANUP_INTERVAL
                
                if paused:
                    await asyncio.sleep(0.2)
                    continue
                
                if now >= next_tick:
                    if await self.poll_state():
                        await self.auto_clean()
                        await self.check_goal()
                    next_tick = now + MAIN_LOOP_INTERVAL
                
                if now >= next_broadcast:
                    await self.msg(self.build_cv())
                    self.log.info(f"Broadcast CV every {BROADCAST_INTERVAL}s")
                    next_broadcast = now + BROADCAST_INTERVAL
            
            except Exception as e:
                self.log.error(f"Loop error: {e}", exc_info=True)
                await asyncio.sleep(1)
        
        await self.cleanup()
        self.log.info("Bot stopped")

def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate configuration and return list of errors."""
    errors = []
    
    required = ['ip', 'port', 'admin_name', 'admin_pass', 'clean_age', 'clean_value', 'goal', 'map']
    for field in required:
        if field not in cfg:
            errors.append(f"Missing required field: {field}")
    
    if 'ip' in cfg and not cfg['ip']:
        errors.append("ip cannot be empty")
    
    if 'port' in cfg:
        port = cfg['port']
        if not isinstance(port, int) or port < 1 or port > 65535:
            errors.append(f"Invalid port: {port}")
    
    for field in ['clean_age', 'clean_value', 'goal']:
        if field in cfg:
            value = cfg[field]
            if not isinstance(value, int) or value < 0:
                errors.append(f"{field} must be a non-negative integer")
    
    if 'map' in cfg and not cfg['map']:
        errors.append("map cannot be empty")
    
    return errors

async def run_bot(cfg: Dict[str, Any], log: logging.Logger) -> None:
    """Run bot with auto-reconnect."""
    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log.error(f"Config error: {error}")
        return
    
    while True:
        try:
            await Bot(cfg, log).run()
            break
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            break
        except Exception as e:
            log.error(f"Bot error: {e}, reconnecting in {RECONNECT_DELAY}s...", exc_info=True)
            await asyncio.sleep(RECONNECT_DELAY)

async def main() -> None:
    """Entry point."""
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    
    try:
        servers = load_config("settings.cfg")
    except FileNotFoundError:
        print("Error: settings.cfg not found")
        return
    except Exception as e:
        print(f"Error loading settings.cfg: {e}")
        return
    
    if not servers:
        print("Error: No servers configured in settings.cfg")
        return
    
    log_level = logging.DEBUG if any(s.get('debug') for s in servers) else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("Bot")
    log.info("=== OpenTTD Admin Bot Starting ===")
    
    tasks = []
    for cfg in servers:
        port = cfg.get('port', 'unknown')
        logger = logging.getLogger(f"Bot[{port}]")
        task = asyncio.create_task(run_bot(cfg, logger))
        tasks.append(task)
    
    log.info(f"Started {len(tasks)} bot instance(s)")
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log.info("Shutting down...")

if __name__ == "__main__":
    asyncio.run(main())
