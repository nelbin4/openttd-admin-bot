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
        
        self.companies: dict[int, dict] = {}
        self.clients: dict[int, dict] = {}
        self.game_date: int | None = None
        self.goal_reached = False
        self.paused: bool | None = None
        self.reset_pending: dict[int, int] = {}

    async def start(self):
        self.admin = AsyncAdmin(self.cfg.server_ip, self.cfg.admin_port)
        await self.admin.login(self.cfg.admin_name, self.cfg.admin_pass)
        await self.admin.subscribe(AdminUpdateType.CHAT)
        await self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
        await self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
        
        self.setup_handlers()
        await self.admin.send_rcon("unpause")
        await self.poll_startup()
        asyncio.create_task(self.poll_economy_loop())
        await self.broadcast("Admin connected")

        while not self.stop.is_set():
            try:
                for packet in await self.admin.recv():
                    await self.admin.handle_packet(packet)
            except Exception as e:
                self.log.error(f"Error: {e}")
            await asyncio.sleep(0.1)

    async def poll(self, update_type: int, data: int):
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet = (3 + len(payload)).to_bytes(2, 'little') + (3).to_bytes(1, 'little') + payload
        self.admin._writer.write(packet)
        await self.admin._writer.drain()

    def setup_handlers(self):
        @self.admin.add_handler(openttdpacket.ChatPacket)
        async def on_chat(admin, pkt):
            if pkt.message.strip().startswith('!'):
                await self.handle_command(pkt.id, pkt.message.strip()[1:])

        @self.admin.add_handler(openttdpacket.DatePacket)
        async def on_date(admin, pkt):
            self.game_date = pkt.date

        @self.admin.add_handler(openttdpacket.CompanyInfoPacket)
        async def on_company_info(admin, pkt):
            cid = 255 if pkt.id == 255 else pkt.id + 1
            self.companies[cid] = {'name': pkt.name, 'founded': pkt.year}
            self.log.info(f"Company #{cid}: {pkt.name}")

        @self.admin.add_handler(openttdpacket.CompanyEconomyPacket)
        async def on_company_economy(admin, pkt):
            cid = 255 if pkt.id == 255 else pkt.id + 1
            if pkt.quarterly_info:
                if cid not in self.companies:
                    self.companies[cid] = {'name': f'Company {cid}', 'founded': 1950}
                self.companies[cid]['value'] = pkt.quarterly_info[0]['company_value']

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        async def on_client_info(admin, pkt):
            cid = 255 if pkt.company_id == 255 else pkt.company_id + 1
            if pkt.name == "<invalid>":
                self.clients.pop(pkt.id, None)
            else:
                self.clients[pkt.id] = {'name': pkt.name, 'company_id': cid, 'ip': pkt.ip}
                if pkt.id in self.reset_pending and self.reset_pending[pkt.id] != cid:
                    self.reset_pending.pop(pkt.id, None)

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        async def on_client_update(admin, pkt):
            cid = 255 if pkt.company_id == 255 else pkt.company_id + 1
            if pkt.id in self.clients:
                self.clients[pkt.id].update({'name': pkt.name, 'company_id': cid})

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        async def on_client_remove(admin, pkt):
            self.clients.pop(pkt.id, None)
            self.reset_pending.pop(pkt.id, None)

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin, pkt):
            cid = 255 if pkt.id == 255 else pkt.id + 1
            self.companies.pop(cid, None)

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        async def on_new_game(admin, pkt):
            self.companies.clear()
            self.clients.clear()
            self.game_date = None
            self.goal_reached = False
            self.paused = None
            await self.poll_startup()

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        async def on_shutdown(admin, pkt):
            self.stop.set()

    async def poll_startup(self):
        await self.poll(AdminUpdateType.DATE.value, 0)
        await self.poll(AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
        for i in range(16):
            await self.poll(AdminUpdateType.COMPANY_INFO.value, i)
            await self.poll(AdminUpdateType.COMPANY_ECONOMY.value, i)
        await asyncio.sleep(2)

    async def poll_economy_loop(self):
        while not self.stop.is_set():
            for cid in list(self.companies.keys()):
                await self.poll(AdminUpdateType.COMPANY_ECONOMY.value, cid - 1 if cid != 255 else 255)
            
            await self.check_dead_companies()
            self.check_goal()
            
            desired = len(self.companies) == 0
            if desired != self.paused:
                await self.admin.send_rcon("pause" if desired else "unpause")
                self.paused = desired
            
            await asyncio.sleep(30)

    def check_goal(self):
        if self.goal_reached:
            return
        for cid, data in self.companies.items():
            if data.get('value', 0) >= self.cfg.goal_value:
                self.goal_reached = True
                asyncio.create_task(self.broadcast(f"{data['name']} WINS! Goal {self.fmt(self.cfg.goal_value)} reached!"))
                asyncio.create_task(self.countdown_reset())
                break

    async def countdown_reset(self):
        for i in range(self.cfg.reset_countdown_seconds, 0, -1):
            await self.broadcast(f"Map resets in {i}s...")
            await asyncio.sleep(1)
        await self.admin.send_rcon(f"load {self.cfg.load_scenario}")
        self.companies.clear()
        self.clients.clear()
        self.goal_reached = False
        await self.broadcast("Map reset!")
        await self.poll_startup()

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
                self.companies.pop(cid, None)
                await self.broadcast(f"Company #{cid} auto-reset (inactive)")

    async def handle_command(self, cid: int, cmd: str):
        parts = cmd.split()
        if not parts:
            return
        
        command = parts[0].lower()
        
        if command == 'help':
            await self.msg("Commands: !info, !rules, !cv, !reset", cid)
        elif command == 'info':
            await self.msg(f"Goal: {self.fmt(self.cfg.goal_value)} company value wins", cid)
        elif command == 'rules':
            await self.msg(f"1. No griefing 2. No cheating 3. Be respectful\nInactive >{self.cfg.dead_co_age}y & <{self.fmt(self.cfg.dead_co_value)} = auto-reset", cid)
        elif command == 'cv':
            if not self.companies:
                await self.msg("No companies", cid)
            else:
                lines = ["=== Company Values ==="]
                for i, (_, data) in enumerate(sorted(self.companies.items(), key=lambda x: x[1].get('value', 0), reverse=True)[:10], 1):
                    lines.append(f"{i}. {data.get('name', 'N/A')}: {self.fmt(data.get('value', 0))}")
                await self.msg('\n'.join(lines), cid)
        elif command == 'reset':
            client = self.clients.get(cid)
            if not client or client.get('company_id') == 255:
                await self.msg("Must be in a company to reset", cid)
            else:
                company_id = client['company_id']
                self.reset_pending[cid] = company_id
                await self.msg(f"Move to spectator within 10s to reset {self.companies.get(company_id, {}).get('name', f'Company {company_id}')}", cid)
                
                async def expire():
                    await asyncio.sleep(10)
                    if self.reset_pending.pop(cid, None) and self.clients.get(cid, {}).get('company_id') == 255:
                        await self.admin.send_rcon(f"reset_company {company_id}")
                        self.companies.pop(company_id, None)
                        await self.msg(f"Company #{company_id} reset", cid)
                
                asyncio.create_task(expire())

    async def msg(self, text: str, cid: int | None = None):
        for line in text.split('\n'):
            if line.strip():
                await self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT if cid else ChatDestTypes.BROADCAST, cid or 0)
                await asyncio.sleep(0.1)

    async def broadcast(self, text: str):
        await self.msg(text)

    def fmt(self, val: int) -> str:
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
    return [Config(
        admin_port=port,
        server_ip=settings["server_ip"],
        admin_name=settings["admin_name"],
        admin_pass=settings["admin_pass"],
        goal_value=settings["goal_value"],
        load_scenario=settings["load_scenario"],
        dead_co_age=settings["dead_co_age"],
        dead_co_value=settings["dead_co_value"],
        reset_countdown_seconds=settings.get("reset_countdown_seconds", 20),
    ) for port in settings.get("admin_ports", [])]


_bots = []


def signal_handler(sig, frame):
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
