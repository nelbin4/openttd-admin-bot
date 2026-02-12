import json
import logging
import signal
import sys
import asyncio
from dataclasses import dataclass
from datetime import date, timedelta, datetime

from aiopyopenttdadmin import Admin as AsyncAdmin
from pyopenttdadmin import openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.enums import *

@dataclass
class Config:
    admin_port: int
    server_ip: str
    admin_name: str
    admin_pass: str
    goal_value: int
    load_scenario: str
    dead_co_age: int
    dead_co_value: int

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger("OpenTTDBot")

class Bot:
    # Init & utilities
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = logging.getLogger(f"[S{cfg.admin_port}]")
        self.admin: AsyncAdmin | None = None
        self.stop = asyncio.Event()

        self.companies: dict[int, dict] = {}
        self.clients: dict[int, dict] = {}
        self.game_date: int | None = None
        self.goal_reached = False
        self.paused: bool | None = None
        self.reset_pending: dict[int, tuple[int, asyncio.Task]] = {}

    def _cancel_reset_pending(self, cid: int):
        entry = self.reset_pending.pop(cid, None)
        if entry:
            _, task = entry
            task.cancel()

    def _set_reset_pending(self, cid: int, company_id: int):
        # Cancel any existing pending reset for this client
        self._cancel_reset_pending(cid)

        async def timeout():
            try:
                await asyncio.sleep(10)
                if cid in self.reset_pending:
                    self.reset_pending.pop(cid, None)
                    await self.msg("Reset cancelled (timeout)", cid)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(timeout())
        self.reset_pending[cid] = (company_id, task)

    def normalize_company_id(self, cid_raw: int) -> int:
        return 255 if cid_raw == 255 else cid_raw + 1

    def fmt(self, val: int) -> str:
        if val >= 1_000_000_000:
            return f"{val/1_000_000_000:.1f}b"
        if val >= 1_000_000:
            return f"{val/1_000_000:.1f}m"
        if val >= 1_000:
            return f"{val/1_000:.1f}k"
        return str(val)

    async def wait_or_stop(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _send_lines(self, text: str, cid: int | None = None):
        for line in text.split('\n'):
            if line.strip():
                await self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT if cid else ChatDestTypes.BROADCAST, cid or 0)
                await asyncio.sleep(0.1)

    # Connection lifecycle
    async def start(self):
        attempt = 0
        while not self.stop.is_set() and attempt < 10:
            try:
                await self.run_bot()
                break
            except Exception as e:
                attempt += 1
                self.log.error(f"Connection error: {e}")

                if attempt >= 10:
                    self.log.error("Max reconnection attempts reached")
                    break

                delay = min(5.0 * attempt, 60)
                self.log.info(f"Reconnecting in {delay}s (attempt {attempt}/10)...")
                await asyncio.sleep(delay)

        self.log.info("Bot stopped")

    async def run_bot(self):
        bg_tasks: list[asyncio.Task] = []
        try:
            async with AsyncAdmin(self.cfg.server_ip, self.cfg.admin_port) as admin:
                self.admin = admin
                await admin.login(self.cfg.admin_name, self.cfg.admin_pass)

                await admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
                await admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
                await admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
                await admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
                await admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.WEEKLY)

                self.setup_handlers()
                await self.poll_all()
                bg_tasks = [
                    asyncio.create_task(self.economy_loop()),
                    asyncio.create_task(self.hourly_cv_loop()),
                ]
                await self.broadcast("Admin connected")

                while not self.stop.is_set():
                    try:
                        for packet in await admin.recv():
                            await admin.handle_packet(packet)
                    except Exception as e:
                        self.log.error(f"Packet handling error: {e}")
                        break
                    await asyncio.sleep(0.1)

                self.log.info("Bot stopping, cleaning up connection...")
        finally:
            for task in bg_tasks:
                task.cancel()
            if bg_tasks:
                await asyncio.gather(*bg_tasks, return_exceptions=True)
            self.admin = None

    # Polling helpers
    async def poll(self, update_type: int, data: int):
        if not self.admin:
            return
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet = (3 + len(payload)).to_bytes(2, 'little') + (3).to_bytes(1, 'little') + payload
        self.admin._writer.write(packet)
        await self.admin._writer.drain()

    async def poll_updates(self, update_types: list[int], ids: list[int]):
        for i in ids:
            for update_type in update_types:
                await self.poll(update_type, i)

    async def poll_all(self):
        await self.poll_updates([AdminUpdateType.DATE.value], [0])
        await self.poll_updates([AdminUpdateType.CLIENT_INFO.value], [0xFFFFFFFF])
        await self.poll_updates([
            AdminUpdateType.COMPANY_INFO.value,
            AdminUpdateType.COMPANY_ECONOMY.value,
        ], list(range(0, 16)))
        await asyncio.sleep(0.5)

    async def poll_economy(self):
        await self.poll_updates([AdminUpdateType.COMPANY_ECONOMY.value], list(range(0, 16)))
        await asyncio.sleep(0.3)

    async def poll_clients(self):
        await self.poll_updates([AdminUpdateType.CLIENT_INFO.value], [0xFFFFFFFF])
        await asyncio.sleep(0.2)

    # Handler wiring
    def setup_handlers(self):
        def register(name: str, *packet_types: type):
            def decorator(fn):
                async def wrapped(admin, pkt):
                    try:
                        await fn(admin, pkt)
                    except Exception as e:
                        self.log.error(f"{name} handler error: {e}")
                self.admin.add_handler(*packet_types)(wrapped)
                return wrapped
            return decorator

        @register("chat", openttdpacket.ChatPacket)
        async def on_chat(admin, pkt):
            if pkt.message.strip().startswith('!'):
                await self.handle_command(pkt.id, pkt.message.strip()[1:])

        @register("date", openttdpacket.DatePacket)
        async def on_date(admin, pkt):
            self.game_date = pkt.date

        @register("company_info", openttdpacket.CompanyInfoPacket)
        async def on_company_info(admin, pkt):
            cid = self.normalize_company_id(pkt.id)
            self.companies[cid] = {'name': pkt.name, 'founded': pkt.year, 'value': 0}
            self.paused = False

        @register("company_economy", openttdpacket.CompanyEconomyPacket)
        async def on_company_economy(admin, pkt):
            cid = self.normalize_company_id(pkt.id)
            if pkt.quarterly_info and cid in self.companies:
                self.companies[cid]['value'] = pkt.quarterly_info[0]['company_value']

        @register("client_info", openttdpacket.ClientInfoPacket)
        async def on_client_info(admin, pkt):
            cid = self.normalize_company_id(pkt.company_id)
            if pkt.name != "<invalid>":
                self.clients[pkt.id] = {'name': pkt.name, 'company_id': cid}

        @register("company_update", openttdpacket.CompanyUpdatePacket)
        async def on_company_update(admin, pkt):
            cid = self.normalize_company_id(pkt.id)
            if cid not in self.companies:
                self.companies[cid] = {'value': 0}
            self.companies[cid]['name'] = pkt.name

        @register("client_update", openttdpacket.ClientUpdatePacket)
        async def on_client_update(admin, pkt):
            cid = self.normalize_company_id(pkt.company_id)
            if pkt.id in self.clients:
                self.clients[pkt.id]['company_id'] = cid

            if pkt.id in self.reset_pending and cid == 255:
                company_id, _ = self.reset_pending.pop(pkt.id)
                await self.admin.send_rcon(f"reset_company {company_id}")
                await self.msg(f"Company #{company_id} reset", pkt.id)

        @register("client_remove", openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        async def on_client_remove(admin, pkt):
            self.clients.pop(pkt.id, None)
            self._cancel_reset_pending(pkt.id)

        @register("company_remove", openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin, pkt):
            cid = self.normalize_company_id(pkt.id)
            self.companies.pop(cid, None)

        @register("new_game", openttdpacket.NewGamePacket)
        async def on_new_game(admin, pkt):
            for _, task in self.reset_pending.values():
                task.cancel()
            self.reset_pending.clear()
            self.companies.clear()
            self.clients.clear()
            self.game_date = None
            self.goal_reached = False
            self.paused = None
            await self.poll_all()

        @register("shutdown", openttdpacket.ShutdownPacket)
        async def on_shutdown(admin, pkt):
            self.stop.set()

    # Loops & state checks
    async def economy_loop(self):
        while not self.stop.is_set():
            if await self.wait_or_stop(30):
                continue

            try:
                await self.poll_economy()
                await self.check_dead_companies()
                await self.check_goal()
                await self.sync_pause()
            except Exception as e:
                self.log.error(f"Economy loop error: {e}")

    async def sync_pause(self):
        desired = len(self.companies) == 0
        if desired != self.paused:
            await self.admin.send_rcon("pause" if desired else "unpause")
            self.paused = desired

    async def check_goal(self):
        if self.goal_reached:
            return

        for cid, data in self.companies.items():
            value = data.get('value', 0)
            if value >= self.cfg.goal_value:
                self.goal_reached = True
                winner_name = data['name']
                self.log.info(f"Goal reached: {winner_name} with {self.fmt(value)}")

                await self.broadcast(f"{winner_name} WINS! Reached {self.fmt(self.cfg.goal_value)} company value!")

                for delay, msg in [(20, "Map resets in 20s..."), (10, "Map resets in 10s..."), (5, "Map resets in 5s...")]:
                    await self.broadcast(msg)
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=delay)
                        return
                    except asyncio.TimeoutError:
                        pass

                await self.admin.send_rcon(f"load {self.cfg.load_scenario}")
                self.companies.clear()
                self.clients.clear()
                self.goal_reached = False
                await self.poll_all()
                break

    async def check_dead_companies(self):
        if not self.game_date:
            return
        year = (date(1, 1, 1) + timedelta(days=self.game_date)).year - 1
        for cid, data in list(self.companies.items()):
            age = year - data.get('founded', 1950)
            if age >= self.cfg.dead_co_age and data.get('value', 0) < self.cfg.dead_co_value:
                for client_id, client in list(self.clients.items()):
                    if client.get('company_id') == cid:
                        await self.admin.send_rcon(f"move {client_id} 255")
                await self.admin.send_rcon(f"reset_company {cid}")
                await self.broadcast(f"Inactive company {data.get('name', 'Unknown')} auto-reset")

    async def hourly_cv_loop(self):
        while not self.stop.is_set():
            now = datetime.now()
            target = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            while not self.stop.is_set():
                remaining = (target - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                if await self.wait_or_stop(min(remaining, 30)):
                    break
            if self.stop.is_set():
                break
            try:
                await self.poll_economy()
                lines = self.build_cv_lines()
                await self.broadcast('\n'.join(lines))
            except Exception as e:
                self.log.error(f"Hourly CV loop error: {e}")

    def build_cv_lines(self) -> list[str]:
        if not self.companies:
            return ["No companies"]
        lines = ["=== Company Values Rankings ==="]
        top = sorted(
            self.companies.items(),
            key=lambda x: x[1].get('value', 0),
            reverse=True,
        )[:10]
        lines.extend(
            f"{i}. {data.get('name', 'N/A')}: {self.fmt(data.get('value', 0))}"
            for i, (_, data) in enumerate(top, 1)
        )
        return lines

    # Commands & messaging
    async def handle_command(self, cid: int, cmd: str):
        parts = cmd.split()
        if not parts:
            return

        commands = {
            'help': self.cmd_help,
            'info': self.cmd_info,
            'rules': self.cmd_rules,
            'cv': self.cmd_cv,
            'reset': self.cmd_reset,
        }
        handler = commands.get(parts[0].lower())
        if handler:
            await handler(cid)

    async def msg(self, text: str, cid: int | None = None):
        await self._send_lines(text, cid)

    async def broadcast(self, text: str):
        await self._send_lines(text)

    async def cmd_help(self, cid: int):
        await self._send_lines("=== Chat Commands ===\n!info, !rules, !cv, !reset", cid)

    async def cmd_info(self, cid: int):
        await self._send_lines(
            f"=== Game Info ===\nGoal: first company to reach {self.fmt(self.cfg.goal_value)} wins\nGamescript: Production Booster\nTransport >70% increases <50% decreases",
            cid,
        )

    async def cmd_rules(self, cid: int):
        await self._send_lines(
            f"=== Server Rules ===\n1. No griefing/sabotage\n2. No blocking players\n3. No cheating/exploits\n4. Be respectful\n5. Inactive companies (>{self.cfg.dead_co_age}yrs & cv <{self.fmt(self.cfg.dead_co_value)}) auto-reset",
            cid,
        )

    async def cmd_cv(self, cid: int):
        await self.poll_economy()
        await self._send_lines('\n'.join(self.build_cv_lines()), cid)

    async def cmd_reset(self, cid: int):
        await self.poll_clients()
        client = self.clients.get(cid)
        if not client or client.get('company_id') == 255:
            await self._send_lines("Must be in a company to reset", cid)
            return
        company_id = client['company_id']
        self._set_reset_pending(cid, company_id)
        await self._send_lines("Move to spectator within 10s to reset", cid)


def load_settings(path: str = "settings.json") -> dict:
    with open(path, "r") as f:
        return json.load(f)


def build_configs(settings: dict) -> list[Config]:
    return [Config(
        admin_port=port,
        server_ip=settings["server_ip"],
        admin_name=settings["admin_name"],
        admin_pass=settings["admin_pass"],
        goal_value=settings["goal_value"],
        load_scenario=settings["load_scenario"],
        dead_co_age=settings["dead_co_age"],
        dead_co_value=settings["dead_co_value"],
    ) for port in settings.get("admin_ports", [])]


_bots = []


def signal_handler(sig, frame):
    for bot in _bots:
        bot.stop.set()
    sys.exit(0)


async def main():
    settings = load_settings()
    configs = build_configs(settings)
    
    if not configs:
        root_logger.error("No admin_ports configured")
        sys.exit(1)
    
    for cfg in configs:
        if cfg.goal_value <= 0:
            root_logger.error(f"Invalid goal_value for port {cfg.admin_port}")
            sys.exit(1)
        if cfg.dead_co_age < 0:
            root_logger.error(f"Invalid dead_co_age for port {cfg.admin_port}")
            sys.exit(1)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    root_logger.info('=== OpenTTD Admin Bot Starting ===')
    
    tasks = []
    for cfg in configs:
        bot = Bot(cfg)
        _bots.append(bot)
        tasks.append(asyncio.create_task(bot.start()))
    
    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
