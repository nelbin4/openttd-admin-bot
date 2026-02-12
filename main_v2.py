import json
import logging
import signal
import sys
import asyncio
from dataclasses import dataclass
from datetime import timedelta, datetime

from aiopyopenttdadmin import Admin as AsyncAdmin
from pyopenttdadmin import openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.enums import Actions, ChatDestTypes, PacketType

SPECTATOR_ID = 255
POLL_ALL_CLIENTS = 0xFFFFFFFF
MAX_COMPANIES = 16

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


class AdminPollPacket(openttdpacket.Packet):
    packet_type = PacketType.ADMIN_POLL

    def __init__(self, update_type: int, data: int):
        self.update_type = update_type
        self.data = data

    def to_bytes(self) -> bytes:
        return self.update_type.to_bytes(1, 'little') + self.data.to_bytes(4, 'little')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger("OpenTTDBot")

class Bot:
    COMMAND_HANDLERS = {
        "help": "cmd_help",
        "info": "cmd_info",
        "rules": "cmd_rules",
        "cv": "cmd_cv",
        "reset": "cmd_reset",
    }

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

    def _reset_state(self):
        self.companies.clear()
        self.clients.clear()
        self.game_date = None
        self.goal_reached = False
        self.paused = None
        for _, task in self.reset_pending.values():
            task.cancel()
        self.reset_pending.clear()

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
                    await self.send_lines("Reset cancelled (timeout)", cid)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(timeout())
        self.reset_pending[cid] = (company_id, task)

    def normalize_company_id(self, cid_raw: int) -> int:
        return SPECTATOR_ID if cid_raw == SPECTATOR_ID else cid_raw + 1

    def fmt(self, val: int) -> str:
        for threshold, suffix in ((1_000_000_000, 'b'), (1_000_000, 'm'), (1_000, 'k')):
            if val >= threshold:
                return f"{val / threshold:.1f}{suffix}"
        return str(val)

    async def wait_or_stop(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _game_year(self) -> int | None:
        if self.game_date is None:
            return None
        return self.game_date // 365

    async def send_lines(self, text: str, cid: int | None = None):
        for line in text.split('\n'):
            if line.strip():
                dest = ChatDestTypes.CLIENT if cid is not None else ChatDestTypes.BROADCAST
                target = cid if cid is not None else 0
                await self.admin._chat(line, Actions.SERVER_MESSAGE, dest, target)
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
            self._reset_state()
            async with AsyncAdmin(self.cfg.server_ip, self.cfg.admin_port) as admin:
                self.admin = admin
                await admin.login(self.cfg.admin_name, self.cfg.admin_pass)

                await admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
                await admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
                await admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
                await admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
                await admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.MONTHLY)

                self.setup_handlers()
                await self.poll_all()
                bg_tasks = [
                    asyncio.create_task(self.economy_loop()),
                    asyncio.create_task(self.hourly_cv_loop()),
                ]
                await self.send_lines("Admin connected")

                while not self.stop.is_set():
                    try:
                        packets = await admin.recv()
                        for packet in packets:
                            await admin.handle_packet(packet)
                        if self.admin and self.admin._reader and self.admin._reader.at_eof():
                            self.log.warning("Connection closed by server")
                            break
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
        await self.admin._send(AdminPollPacket(update_type, data))

    async def poll_updates(self, update_types: list[int], ids: list[int]):
        for i in ids:
            for update_type in update_types:
                await self.poll(update_type, i)

    async def poll_all(self):
        await self.poll_updates([AdminUpdateType.DATE.value], [0])
        await self.poll_updates([AdminUpdateType.CLIENT_INFO.value], [POLL_ALL_CLIENTS])
        await self.poll_updates([
            AdminUpdateType.COMPANY_INFO.value,
            AdminUpdateType.COMPANY_ECONOMY.value,
        ], list(range(0, MAX_COMPANIES)))
        await asyncio.sleep(0.5)

    async def poll_economy(self):
        await self.poll_updates([AdminUpdateType.COMPANY_ECONOMY.value], list(range(0, MAX_COMPANIES)))
        await asyncio.sleep(0.3)

    async def poll_clients(self):
        await self.poll_updates([AdminUpdateType.CLIENT_INFO.value], [POLL_ALL_CLIENTS])
        await asyncio.sleep(0.2)

    # Handler wiring
    def setup_handlers(self):
        handlers = {
            openttdpacket.ChatPacket: self._on_chat,
            openttdpacket.DatePacket: self._on_date,
            openttdpacket.CompanyInfoPacket: self._on_company_info,
            openttdpacket.CompanyEconomyPacket: self._on_company_economy,
            openttdpacket.ClientInfoPacket: self._on_client_info,
            openttdpacket.CompanyUpdatePacket: self._on_company_update,
            openttdpacket.ClientUpdatePacket: self._on_client_update,
            openttdpacket.ClientQuitPacket: self._on_client_remove,
            openttdpacket.ClientErrorPacket: self._on_client_remove,
            openttdpacket.CompanyRemovePacket: self._on_company_remove,
            openttdpacket.NewGamePacket: self._on_new_game,
            openttdpacket.ShutdownPacket: self._on_shutdown,
        }
        for pkt_type, handler in handlers.items():
            async def wrapped(admin, pkt, fn=handler, name=pkt_type.__name__):
                try:
                    await fn(pkt)
                except Exception as exc:
                    self.log.error(f"{name} handler error: {exc}")
            self.admin.add_handler(pkt_type)(wrapped)

    async def _on_chat(self, pkt):
        if pkt.message.strip().startswith('!'):
            await self.handle_command(pkt.id, pkt.message.strip()[1:])

    async def _on_date(self, pkt):
        self.game_date = pkt.date

    async def _on_company_info(self, pkt):
        cid = self.normalize_company_id(pkt.id)
        existing = self.companies.get(cid, {})
        existing.update({'name': pkt.name, 'founded': pkt.year})
        existing.setdefault('value', 0)
        self.companies[cid] = existing

    async def _on_company_economy(self, pkt):
        cid = self.normalize_company_id(pkt.id)
        if pkt.quarterly_info and cid in self.companies:
            self.companies[cid]['value'] = pkt.quarterly_info[0]['company_value']

    async def _on_client_info(self, pkt):
        cid = self.normalize_company_id(pkt.company_id)
        if pkt.name != "<invalid>":
            self.clients[pkt.id] = {'name': pkt.name, 'company_id': cid}

    async def _on_company_update(self, pkt):
        cid = self.normalize_company_id(pkt.id)
        if cid not in self.companies:
            current_year = self._game_year()
            self.companies[cid] = {'value': 0, 'founded': current_year}
        self.companies[cid]['name'] = pkt.name

    async def _on_client_update(self, pkt):
        cid = self.normalize_company_id(pkt.company_id)
        if pkt.id in self.clients:
            self.clients[pkt.id]['company_id'] = cid
            self.clients[pkt.id]['name'] = pkt.name

        if pkt.id in self.reset_pending and cid == SPECTATOR_ID:
            company_id, task = self.reset_pending.pop(pkt.id)
            task.cancel()
            await self.admin.send_rcon(f"reset_company {company_id}")
            await self.send_lines(f"Company #{company_id} reset", pkt.id)

    async def _on_client_remove(self, pkt):
        self.clients.pop(pkt.id, None)
        self._cancel_reset_pending(pkt.id)

    async def _on_company_remove(self, pkt):
        cid = self.normalize_company_id(pkt.id)
        self.companies.pop(cid, None)

    async def _on_new_game(self, _pkt):
        self._reset_state()
        await self.poll_all()

    async def _on_shutdown(self, _pkt):
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
                winner_name = data.get('name', 'Unknown')
                self.log.info(f"Goal reached: {winner_name} with {self.fmt(value)}")

                await self.send_lines(f"{winner_name} WINS! Reached {self.fmt(self.cfg.goal_value)} company value!")

                for delay, msg in [(10, "Map resets in 20s..."), (5, "Map resets in 10s..."), (5, "Map resets in 5s...")]:
                    await self.send_lines(msg)
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=delay)
                        return
                    except asyncio.TimeoutError:
                        pass

                await self.admin.send_rcon(f"load {self.cfg.load_scenario}")
                break

    async def check_dead_companies(self):
        if self.game_date is None:
            return
        year = self._game_year()
        if year is None:
            return
        for cid, data in list(self.companies.items()):
            founded = data.get('founded')
            if founded is None:
                continue
            age = year - founded
            if age >= self.cfg.dead_co_age and data.get('value', 0) < self.cfg.dead_co_value:
                for client_id, client in list(self.clients.items()):
                    if client.get('company_id') == cid:
                        await self.admin.send_rcon(f"move {client_id} {SPECTATOR_ID}")
                await self.admin.send_rcon(f"reset_company {cid}")
                await self.send_lines(f"Inactive company {data.get('name', 'Unknown')} auto-reset")

    async def hourly_cv_loop(self):
        while not self.stop.is_set():
            now = datetime.now()
            target = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            remaining = (target - datetime.now()).total_seconds()
            if await self.wait_or_stop(max(remaining, 0)):
                break
            try:
                lines = self.build_cv_lines()
                await self.send_lines('\n'.join(lines))
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

        handler_name = self.COMMAND_HANDLERS.get(parts[0].lower())
        if handler_name:
            await getattr(self, handler_name)(cid)

    async def cmd_help(self, cid: int):
        await self.send_lines("=== Chat Commands ===\n!info, !rules, !cv, !reset", cid)

    async def cmd_info(self, cid: int):
        await self.send_lines(
            f"=== Game Info ===\nGoal: first company to reach {self.fmt(self.cfg.goal_value)} wins\nGamescript: Production Booster\nTransport >70% increases <50% decreases",
            cid,
        )

    async def cmd_rules(self, cid: int):
        await self.send_lines(
            f"=== Server Rules ===\n1. No griefing/sabotage\n2. No blocking players\n3. No cheating/exploits\n4. Be respectful\n5. Inactive companies (>{self.cfg.dead_co_age}yrs & cv <{self.fmt(self.cfg.dead_co_value)}) auto-reset",
            cid,
        )

    async def cmd_cv(self, cid: int):
        await self.send_lines('\n'.join(self.build_cv_lines()), cid)

    async def cmd_reset(self, cid: int):
        client = self.clients.get(cid)
        if not client or client.get('company_id') == SPECTATOR_ID:
            await self.send_lines("Must be in a company to reset", cid)
            return
        company_id = client['company_id']
        self._set_reset_pending(cid, company_id)
        await self.send_lines("Move to spectator within 10s to reset", cid)


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
    
    loop = asyncio.get_running_loop()
    bots: list[Bot] = []

    def stop_bots():
        for bot in bots:
            bot.stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_bots)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop_bots())
    root_logger.info('=== OpenTTD Admin Bot Starting ===')
    
    tasks = []
    for cfg in configs:
        bot = Bot(cfg)
        bots.append(bot)
        tasks.append(asyncio.create_task(bot.start()))

    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
