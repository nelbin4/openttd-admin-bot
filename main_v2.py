import json
import logging
import signal
import sys
import asyncio
from dataclasses import dataclass
from datetime import date, timedelta

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
    reset_countdown_seconds: int


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger("OpenTTDBot")


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = logging.getLogger(f"[S{cfg.admin_port}]")
        self.admin: AsyncAdmin | None = None
        self.stop = asyncio.Event()

        # Store normalized (1-based) IDs for companies/clients as soon as packets arrive
        self.companies: dict[int, dict] = {}
        self.clients: dict[int, dict] = {}
        self.game_date: int | None = None
        self.goal_reached = False
        self.paused = False
        self.reset_pending: dict[int, int] = {}

    async def start(self):
        self.admin = AsyncAdmin(self.cfg.server_ip, self.cfg.admin_port)
        # alias .on to match local usage preference
        self.admin.on = self.admin.add_handler

        await self.admin.login(self.cfg.admin_name, self.cfg.admin_pass)

        await self.admin.subscribe(AdminUpdateType.CHAT)
        await self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
        await self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.WEEKLY)

        self.setup_handlers()
        await self.poll_startup()
        await self.broadcast("Admin connected")

        while not self.stop.is_set():
            packets = await self.admin.recv()
            for packet in packets:
                await self.admin.handle_packet(packet)

            self.check_goal()
            await self.sync_pause()
            await asyncio.sleep(0.1)

    async def poll(self, update_type: int, data: int):
        if not self.admin or not self.admin._writer:
            raise ValueError("Admin not connected")
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet_size = 3 + len(payload)
        packet = packet_size.to_bytes(2, 'little') + (3).to_bytes(1, 'little') + payload
        self.admin._writer.write(packet)
        await self.admin._writer.drain()

    def normalize_company_id(self, company_id: int) -> int:
        return 255 if company_id == 255 else company_id + 1

    def normalize_client_id(self, client_id: int) -> int:
        return client_id + 1

    def setup_handlers(self):
        @self.admin.on(openttdpacket.ChatPacket)
        async def on_chat(admin, pkt):
            msg = pkt.message.strip()
            cid = self.normalize_client_id(pkt.id)
            if msg.startswith('!'):
                await self.handle_command(cid, msg[1:])

        @self.admin.on(openttdpacket.DatePacket)
        async def on_date(admin, pkt):
            self.game_date = pkt.date

        @self.admin.on(openttdpacket.CompanyInfoPacket)
        async def on_company_info(admin, pkt):
            company_id = self.normalize_company_id(pkt.id)
            founded = pkt.year
            self.companies[company_id] = {
                'name': pkt.name,
                'founded': founded
            }
            self.log.info(f"Company #{company_id}: {pkt.name} (founded {founded})")

        @self.admin.on(openttdpacket.CompanyEconomyPacket)
        async def on_company_economy(admin, pkt):
            company_id = self.normalize_company_id(pkt.id)
            if company_id not in self.companies:
                self.companies[company_id] = {'name': f'Company {company_id}', 'founded': 1950}

            value = pkt.quarterly_info[-1]['company_value']
            self.companies[company_id]['value'] = value
            self.check_dead_companies()

        @self.admin.on(openttdpacket.ClientInfoPacket)
        async def on_client_info(admin, pkt):
            cid = self.normalize_client_id(pkt.id)
            company_id = self.normalize_company_id(pkt.company_id)

            if pkt.name == "<invalid>":
                self.clients.pop(cid, None)
            else:
                self.clients[cid] = {
                    'name': pkt.name,
                    'company_id': company_id,
                    'ip': pkt.ip
                }

                if cid in self.reset_pending and self.reset_pending[cid] != company_id:
                    self.reset_pending.pop(cid, None)

            await self.sync_pause()

        @self.admin.on(openttdpacket.ClientUpdatePacket)
        async def on_client_update(admin, pkt):
            cid = self.normalize_client_id(pkt.id)
            company_id = self.normalize_company_id(pkt.company_id)

            if cid in self.clients:
                self.clients[cid].update({'name': pkt.name, 'company_id': company_id})
            await self.sync_pause()

        @self.admin.on(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        async def on_client_remove(admin, pkt):
            cid = self.normalize_client_id(pkt.id)

            self.clients.pop(cid, None)
            self.reset_pending.pop(cid, None)
            await self.sync_pause()

        @self.admin.on(openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin, pkt):
            company_id = self.normalize_company_id(pkt.id)

            self.companies.pop(company_id, None)
            self.log.info(f"Company #{company_id} removed")

        @self.admin.on(openttdpacket.NewGamePacket)
        async def on_new_game(admin, pkt):
            self.log.info("New game detected; re-syncing state")
            self.companies.clear()
            self.clients.clear()
            self.game_date = None
            self.goal_reached = False
            self.paused = False
            await self.poll_startup()

        @self.admin.on(openttdpacket.ShutdownPacket)
        async def on_shutdown(admin, pkt):
            self.log.warning("Server shutdown packet received; stopping bot")
            self.stop.set()

    async def poll_startup(self):
        self.log.info("Polling initial data...")
        await self.poll(AdminUpdateType.DATE.value, 0)
        await self.poll(AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)

        for packet_id in range(16):
            await self.poll(AdminUpdateType.COMPANY_INFO.value, packet_id)
            await self.poll(AdminUpdateType.COMPANY_ECONOMY.value, packet_id)

        await asyncio.sleep(2)

    def check_goal(self):
        if self.goal_reached:
            return

        for company_id, data in self.companies.items():
            if data.get('value', 0) >= self.cfg.goal_value:
                self.goal_reached = True
                asyncio.create_task(self.broadcast(
                    f"{data['name']} WINS! Goal {self.fmt(self.cfg.goal_value)} reached!"
                ))
                asyncio.create_task(self.countdown_reset())
                return

    async def countdown_reset(self):
        for i in range(self.cfg.reset_countdown_seconds, 0, -1):
            await self.broadcast(f"Map resets in {i}s...")
            await asyncio.sleep(1)

        await self.admin.send_rcon(f"load {self.cfg.load_scenario}")
        self.companies.clear()
        self.goal_reached = False
        await self.broadcast("Map reset!")
        await self.poll_startup()

    def check_dead_companies(self):
        if not self.game_date or not self.companies:
            return

        current_year = (date(1, 1, 1) + timedelta(days=self.game_date)).year - 1

        for company_id, data in list(self.companies.items()):
            age = current_year - data.get('founded', 1950)
            value = data.get('value', 0)

            if age >= self.cfg.dead_co_age and value < self.cfg.dead_co_value:
                self.log.info(f"Auto-reset Company #{company_id}: age={age}y, value={self.fmt(value)}")

                for cid, client in list(self.clients.items()):
                    if client.get('company_id') == company_id:
                        asyncio.create_task(self.admin.send_rcon(f"move {cid} 255"))

                asyncio.create_task(self.admin.send_rcon(f"reset_company {company_id}"))
                self.companies.pop(company_id, None)
                asyncio.create_task(self.broadcast(f"Company #{company_id} auto-reset (inactive)"))

    async def sync_pause(self):
        should_pause = len(self.companies) == 0

        if should_pause != self.paused:
            await self.admin.send_rcon("pause" if should_pause else "unpause")
            self.paused = should_pause

    async def handle_command(self, cid: int, cmd: str):
        parts = cmd.split()
        if not parts:
            return

        command = parts[0].lower()

        if command == 'help':
            await self.msg("Commands: !info, !rules, !cv, !reset", cid)

        elif command == 'info':
            await self.msg(
                f"=== Info ===\n"
                f"Goal: {self.fmt(self.cfg.goal_value)} company value wins\n"
                f"Production Booster: >70% transported = boost, <50% = reduce",
                cid
            )

        elif command == 'rules':
            await self.msg(
                f"=== Rules ===\n"
                f"1. No griefing/blocking\n"
                f"2. No cheating\n"
                f"3. Be respectful\n"
                f"4. Inactive >{self.cfg.dead_co_age}y & <{self.fmt(self.cfg.dead_co_value)} = auto-reset",
                cid
            )

        elif command == 'cv':
            await self.show_company_values(cid)

        elif command == 'reset':
            await self.handle_reset(cid)

    async def show_company_values(self, cid: int):
        if not self.companies:
            await self.msg("No companies", cid)
            return

        lines = ["=== Company Values ==="]
        sorted_cos = sorted(self.companies.items(), key=lambda x: x[1].get('value', 0), reverse=True)[:10]

        for i, (game_id, data) in enumerate(sorted_cos, 1):
            name = data.get('name', f'Company {game_id}')
            value = self.fmt(data.get('value', 0))
            lines.append(f"{i}. {name}: {value}")

        await self.msg('\n'.join(lines), cid)

    async def handle_reset(self, cid: int):
        client = self.clients.get(cid)
        if not client or client.get('company_id') == 255:
            await self.msg("Must be in a company to reset", cid)
            return

        company_id = client['company_id']
        self.reset_pending[cid] = company_id
        company_name = self.companies.get(company_id, {}).get('name', f"Company {company_id}")

        await self.msg(
            f"Move to spectator within 10s to proceed with reset: {company_name}.",
            cid
        )

        async def expire():
            await asyncio.sleep(10)
            pending_company = self.reset_pending.pop(cid, None)
            if pending_company is None:
                return

            current_company = self.clients.get(cid, {}).get('company_id', 255)

            if current_company == 255:
                await self.admin.send_rcon(f"reset_company {pending_company}")
                self.companies.pop(pending_company, None)
                await self.msg(f"Company #{pending_company} reset", cid)

                await self.sync_pause()
            elif current_company != pending_company:
                await self.msg("Reset cancelled (company changed)", cid)
            else:
                await self.msg("Reset cancelled (timeout)", cid)

        asyncio.create_task(expire())

    async def msg(self, text: str, cid: int | None = None):
        """Send message to client or broadcast"""
        for line in text.split('\n'):
            if not line.strip():
                continue

            if cid is not None:
                await self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT, cid)
            else:
                await self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.BROADCAST)

            await asyncio.sleep(0.1)

    async def broadcast(self, text: str):
        await self.msg(text)

    def fmt(self, val: int) -> str:
        """Format large numbers"""
        if val >= 1_000_000_000:
            return f"{val/1_000_000_000:.1f}b"
        if val >= 1_000_000:
            return f"{val/1_000_000:.1f}m"
        if val >= 1_000:
            return f"{val/1_000:.1f}k"
        return str(val)

def load_settings(path: str = "settings.json") -> dict:
    with open(path, "r") as f:
        return json.load(f)

def build_configs(settings: dict) -> list[Config]:
    return [
        Config(
            admin_port=port,
            server_ip=settings["server_ip"],
            admin_name=settings["admin_name"],
            admin_pass=settings["admin_pass"],
            goal_value=settings["goal_value"],
            load_scenario=settings["load_scenario"],
            dead_co_age=settings["dead_co_age"],
            dead_co_value=settings["dead_co_value"],
            reset_countdown_seconds=settings.get("reset_countdown_seconds", 20),
        )
        for port in settings.get("admin_ports", [])
    ]

_bots = []

def signal_handler(sig, frame):
    root_logger.info("Shutting down...")
    for bot in _bots:
        bot.stop.set()
    sys.exit(0)

async def main():
    settings = load_settings()
    configs = build_configs(settings)
    
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
