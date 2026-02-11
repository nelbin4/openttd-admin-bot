import json
import logging
import signal
import sys
import asyncio
from dataclasses import dataclass
from datetime import date, timedelta

from pyopenttdadmin import openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.packet import *
from pyopenttdadmin.enums import *

class Admin:
    def __init__(self, ip: str = "127.0.0.1", port: int = 3977):
        self.ip = ip
        self.port = port
        self._buffer = b""
        self._packets = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.handlers: dict[type, list] = {}
    
    async def connect(self):
        self._reader, self._writer = await asyncio.open_connection(self.ip, self.port)
    
    async def login(self, name: str, password: str):
        packet = AdminJoinPacket(password, name, "0")
        await self.send(packet)
    
    async def send(self, packet: Packet):
        if not self._writer:
            raise ValueError("Not connected")
        
        data = packet.to_bytes()
        packet_type = packet.packet_type.value.to_bytes(1, 'little')
        length = (len(data) + 3).to_bytes(2, 'little')
        self._writer.write(length + packet_type + data)
        await self._writer.drain()
    
    async def recv(self) -> list[Packet]:
        if not self._reader:
            raise ValueError("Not connected")
        
        self._buffer += await self._reader.read(1024)
        if not self._buffer:
            return []
        
        packets = self._packets
        self._packets = []

        fetched = 1
        while True:
            if len(self._buffer) < 2:
                return packets
            
            packet_len = int.from_bytes(self._buffer[0:2], 'little')
            if len(self._buffer) < packet_len:
                if fetched > 5:
                    return packets
                self._buffer += await self._reader.read(1024)
                fetched += 1
                continue
            
            packets.append(Packet.create_packet(self._buffer[2:packet_len]))
            self._buffer = self._buffer[packet_len:]
            if not self._buffer:
                return packets
    
    async def poll(self, update_type: int, data: int):
        """Send manual poll packet (type 3)"""
        PACKET_TYPE = 3
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet_size = 3 + len(payload)
        packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
        self._writer.write(packet)
        await self._writer.drain()
    
    async def rcon(self, command: str):
        await self.send(AdminRconPacket(command))
    
    async def chat(self, message: str, action=Actions.CHAT, desttype=ChatDestTypes.BROADCAST, client_id=0):
        await self.send(AdminChatPacket(message, action, desttype, client_id))
    
    async def subscribe(self, update_type: AdminUpdateType, freq=AdminUpdateFrequency.AUTOMATIC):
        if freq not in AdminUpdateTypeFrequencyMatrix[update_type]:
            raise ValueError(f"Invalid frequency for {update_type}")
        await self.send(AdminSubscribePacket(update_type, freq))
    
    async def handle_packet(self, packet: Packet):
        tasks = [h(self, packet) for h in self.handlers.get(type(packet), [])]
        if tasks:
            await asyncio.gather(*tasks)
    
    def on(self, *packet_types):
        def decorator(func):
            for ptype in packet_types:
                self.handlers.setdefault(ptype, []).append(func)
            return func
        return decorator

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
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = logging.getLogger(f"[S{cfg.admin_port}]")
        self.admin = None
        self.stop = asyncio.Event()
        
        # IMPORTANT: Store companies/clients using GAME IDs (1-based), not packet IDs (0-based)
        self.companies = {}  # game_id -> {name, founded, value, ...}
        self.clients = {}    # client_id -> {name, company_id (game), ip}
        self.game_date = None
        self.goal_reached = False
        self.paused = False
        self.reset_pending = {}  # client_id -> company_id (game)
    
    def packet_to_game_id(self, packet_id: int) -> int:
        """Convert 0-based packet ID to 1-based game ID"""
        return packet_id + 1
    
    async def start(self):
        self.admin = Admin(self.cfg.server_ip, self.cfg.admin_port)
        await self.admin.connect()
        await self.admin.login(self.cfg.admin_name, self.cfg.admin_pass)
        
        # Subscribe to updates
        await self.admin.subscribe(AdminUpdateType.CHAT)
        await self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
        await self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.MONTHLY)
        
        self.setup_handlers()
        await self.poll_startup()
        await self.broadcast("Admin connected")
        
        # Main loop
        while not self.stop.is_set():
            packets = await self.admin.recv()
            for packet in packets:
                await self.admin.handle_packet(packet)
            
            self.check_goal()
            await self.sync_pause()
            await asyncio.sleep(0.1)
    
    def setup_handlers(self):
        @self.admin.on(openttdpacket.ChatPacket)
        async def on_chat(admin, pkt):
            msg = pkt.message.strip()
            if msg.startswith('!'):
                await self.handle_command(pkt.id, msg[1:])
        
        @self.admin.on(openttdpacket.DatePacket)
        async def on_date(admin, pkt):
            self.game_date = pkt.date
        
        @self.admin.on(openttdpacket.CompanyInfoPacket)
        async def on_company_info(admin, pkt):
            game_id = self.packet_to_game_id(pkt.id)
            founded = getattr(pkt, 'year', 1950) or 1950
            
            self.companies[game_id] = {
                'name': pkt.name,
                'founded': founded
            }
            self.log.info(f"Company #{game_id}: {pkt.name} (founded {founded})")
        
        @self.admin.on(openttdpacket.CompanyEconomyPacket)
        async def on_company_economy(admin, pkt):
            game_id = self.packet_to_game_id(pkt.id)
            
            if game_id not in self.companies:
                self.companies[game_id] = {'name': f'Company {game_id}', 'founded': 1950}
            
            # Handle different packet formats
            if getattr(pkt, 'quarterly_info', None):
                value = pkt.quarterly_info[-1]['company_value']
            elif hasattr(pkt, 'company_value'):
                value = pkt.company_value
            else:
                return
            
            self.companies[game_id]['value'] = value
            self.check_dead_companies()
        
        @self.admin.on(openttdpacket.ClientInfoPacket)
        async def on_client_info(admin, pkt):
            cid = pkt.id
            company_id = getattr(pkt, 'play_as', getattr(pkt, 'company_id', 255))
            
            # Convert to game ID if not spectator
            if company_id != 255:
                company_id = self.packet_to_game_id(company_id)
            
            if pkt.name == "<invalid>":
                self.clients.pop(cid, None)
            else:
                self.clients[cid] = {
                    'name': pkt.name,
                    'company_id': company_id,
                    'ip': getattr(pkt, 'ip', 'N/A')
                }
                
                # Cancel pending reset if company changed
                if cid in self.reset_pending and self.reset_pending[cid] != company_id:
                    self.reset_pending.pop(cid, None)
            
            await self.sync_pause()
        
        @self.admin.on(openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin, pkt):
            game_id = self.packet_to_game_id(pkt.id)
            self.companies.pop(game_id, None)
            self.log.info(f"Company #{game_id} removed")
    
    async def poll_startup(self):
        """Poll for initial server state (like test.py)"""
        self.log.info("Polling initial data...")
        await self.admin.poll(AdminUpdateType.DATE.value, 0)
        await self.admin.poll(AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
        
        # Poll all possible companies (0-15 in packet space)
        for packet_id in range(16):
            await self.admin.poll(AdminUpdateType.COMPANY_INFO.value, packet_id)
            await self.admin.poll(AdminUpdateType.COMPANY_ECONOMY.value, packet_id)
        
        await asyncio.sleep(2)  # Wait for responses
    
    def check_goal(self):
        if self.goal_reached:
            return
        
        for game_id, data in self.companies.items():
            if data.get('value', 0) >= self.cfg.goal_value:
                self.goal_reached = True
                asyncio.create_task(self.broadcast(
                    f"{data['name']} WINS! Goal {self.fmt(self.cfg.goal_value)} reached!"
                ))
                asyncio.create_task(self.countdown_reset())
                return
    
    async def countdown_reset(self):
        for i in range(20, 0, -1):
            await self.broadcast(f"Map resets in {i}s...")
            await asyncio.sleep(1)
        
        await self.admin.rcon(f"load {self.cfg.load_scenario}")
        self.companies.clear()
        self.goal_reached = False
        await self.broadcast("Map reset!")
    
    def check_dead_companies(self):
        if not self.game_date or not self.companies:
            return
        
        current_year = (date(1, 1, 1) + timedelta(days=self.game_date)).year - 1
        
        for game_id, data in list(self.companies.items()):
            age = current_year - data.get('founded', 1950)
            value = data.get('value', 0)
            
            if age >= self.cfg.dead_co_age and value < self.cfg.dead_co_value:
                self.log.info(f"Auto-reset Company #{game_id}: age={age}y, value={self.fmt(value)}")
                
                # Move clients out first
                for cid, client in self.clients.items():
                    if client.get('company_id') == game_id:
                        game_client_id = cid + 1
                        asyncio.create_task(self.admin.rcon(f"move {game_client_id} 255"))
                
                asyncio.create_task(self.admin.rcon(f"reset_company {game_id}"))
                self.companies.pop(game_id, None)
                asyncio.create_task(self.broadcast(f"Company #{game_id} auto-reset (inactive)"))
    
    async def sync_pause(self):
        active = [c for c in self.clients.values() if c.get('company_id') != 255]
        should_pause = len(self.companies) == 0
        
        if should_pause != self.paused:
            await self.admin.rcon("pause" if should_pause else "unpause")
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
        
        elif command == 'yes':
            await self.confirm_reset(cid)
    
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
        await self.msg(
            f"=== Reset Company #{company_id} ===\n"
            f"This DELETES your company!\n"
            f"Type !yes to confirm (10s timeout)",
            cid
        )
        
        async def expire():
            await asyncio.sleep(10)
            self.reset_pending.pop(cid, None)
        asyncio.create_task(expire())
    
    async def confirm_reset(self, cid: int):
        if cid not in self.reset_pending:
            return
        
        company_id = self.reset_pending.pop(cid)
        client = self.clients.get(cid)
        
        if not client or client.get('company_id') not in (company_id, 255):
            await self.msg("Reset cancelled (company changed)", cid)
            return
        
        game_client_id = cid + 1
        await self.admin.rcon(f"move {game_client_id} 255")
        await asyncio.sleep(0.2)
        
        if self.clients.get(cid, {}).get('company_id') != 255:
            await self.msg("Could not move to spectators", cid)
            return
        
        await self.admin.rcon(f"reset_company {company_id}")
        self.companies.pop(company_id, None)
        await self.msg(f"Company #{company_id} reset", cid)
        await self.sync_pause()
    
    async def msg(self, text: str, cid: int = None):
        """Send message to client or broadcast"""
        for line in text.split('\n'):
            if not line.strip():
                continue
            
            if cid:
                await self.admin.chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT, cid)
            else:
                await self.admin.chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.BROADCAST)
            
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
