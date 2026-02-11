import json
import logging
import signal
import sys
import asyncio
import time
import inspect
from dataclasses import dataclass

from pyopenttdadmin import openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.packet import *
from pyopenttdadmin.enums import *
from datetime import date, timedelta
from typing import Callable, Coroutine

class Admin:
    def __init__(self, ip: str = "127.0.0.1", port: int = 3977):
        self.ip = ip
        self.port = port
        self._buffer = b""
        self._packets = []

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        self.handlers: dict[PacketType, list[Callable[[Admin, Packet], Coroutine]]] = {}
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_value, traceback):
        if self._writer is not None:
            if not self._writer.is_closing():
                self._writer.close()
            
            await self._writer.wait_closed()
    
    async def connect(self):
        self._reader, self._writer = await asyncio.open_connection(self.ip, self.port)
    
    async def login(self, name: str, password: str, version: int = 0):
        if self._writer is None:
            await self.connect()
        
        packet = AdminJoinPacket(password, name, str(version))
        await self._send(packet)
    
    async def _send(self, packet: Packet):
        if self._writer is None:
            raise ValueError("Not connected to server.")
        
        data = packet.to_bytes()
        packet_type = packet.packet_type.value.to_bytes(1, 'little')
        length = (len(data) + 3).to_bytes(2, 'little')

        self._writer.write(length + packet_type + data)
        await self._writer.drain()
    
    async def recv(self) -> list[Packet]:
        if self._reader is None:
            raise ValueError("Not connected to server.")
        
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
            
            packets.append(Packet.create_packet(self._buffer[2: packet_len]))
            self._buffer = self._buffer[packet_len:]

            if not self._buffer:
                return packets
        
    async def _rcon(self, command: str):
        packet = AdminRconPacket(command)
        await self._send(packet)
    
    async def _chat(self, message: str, action: Actions = Actions.CHAT, desttype: ChatDestTypes = ChatDestTypes.BROADCAST, id: int = 0):
        packet = AdminChatPacket(message, action, desttype, id)
        await self._send(packet)
    
    async def _subscribe(self, type: AdminUpdateType, frequency: AdminUpdateFrequency = AdminUpdateFrequency.AUTOMATIC):
        packet = AdminSubscribePacket(type, frequency)
        await self._send(packet)
    
    async def send_rcon(self, command: str) -> None:
        await self._rcon(command)
    
    async def send_global(self, message: str) -> None:
        await self._chat(message)

    async def subscribe(self, type: AdminUpdateType, frequency: AdminUpdateFrequency = AdminUpdateFrequency.AUTOMATIC) -> None:
        if frequency not in AdminUpdateTypeFrequencyMatrix[type]:
            raise ValueError(f"Invalid frequency ({frequency}) for {type}")
        
        await self._subscribe(type, frequency)
    
    async def handle_packet(self, packet: Packet):
        tasks = set()
        for handler in self.handlers.get(type(packet), []):
            tasks.add(handler(self, packet))
        
        await asyncio.gather(*tasks)
    
    def add_handler(self, *packets: type[Packet]):
        def decorator(func: Callable[[Admin, Packet], Coroutine]):
            if not inspect.iscoroutinefunction(func):
                raise ValueError("Handler must be a coroutine.")

            for packet_type in packets:
                if packet_type not in self.handlers:
                    self.handlers[packet_type] = []
                self.handlers[packet_type].append(func)
            
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
    reset_countdown_seconds: int

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger("OpenTTDBot")

class AsyncRconBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = logging.getLogger(f"[S{cfg.admin_port}]")
        self.admin = None
        self.stop_event = asyncio.Event()
        self.companies = {}
        self.clients = {}
        self.game_date = None
        self.goal_reached = False
        self.paused = False
        self.reset_pending = {}
        self.cleanup_pending = set()
        self.data_received = False
        
    async def start(self):
        self.admin = Admin(self.cfg.server_ip, self.cfg.admin_port)
        await self.admin.connect()
        await self.admin.login(self.cfg.admin_name, self.cfg.admin_pass)
        
        await self.admin.subscribe(openttdpacket.AdminUpdateType.CHAT)
        await self.admin.subscribe(openttdpacket.AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
        await self.admin.subscribe(openttdpacket.AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(openttdpacket.AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
        await self.admin.subscribe(openttdpacket.AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.MONTHLY)
        
        self.admin.add_handler(openttdpacket.ChatPacket)(self.on_chat)
        self._setup_handlers()
        await self._poll_startup()
        
        await self._broadcast("Admin connected")
        
        while not self.stop_event.is_set():
            try:
                packets = await self.admin.recv()
                for packet in packets:
                    await self.admin.handle_packet(packet)
                    self.data_received = True
            except:
                pass
            
            self._check_goal()
            await self._sync_pause_state()
            await asyncio.sleep(0.1)
    
    async def on_chat(self, admin, pkt):
        msg = pkt.message.strip()
        cid = pkt.id
        self.logger.info(f"[CHAT] Client {cid}: {msg}")
        
        if msg.startswith('!'):
            await self._handle_command(cid, msg[1:])
    
    def _setup_handlers(self):
        self.admin.add_handler(openttdpacket.DatePacket)(self._on_date)
        self.admin.add_handler(openttdpacket.CompanyInfoPacket)(self._on_company_info)
        self.admin.add_handler(openttdpacket.CompanyEconomyPacket)(self._on_company_economy)
        self.admin.add_handler(openttdpacket.ClientInfoPacket)(self._on_client_info)
        self.admin.add_handler(openttdpacket.CompanyRemovePacket)(self._on_company_remove)

    async def _on_date(self, _admin, packet):
        self.game_date = packet.date
        self.logger.info(f"Received date: {self.game_date} ({self._fmt_date(self.game_date)})")

    def _co_id(self, packet_id: int) -> int:
        return packet_id + 1

    async def _on_company_info(self, _admin, packet):
        co_id = self._co_id(packet.id)
        company_name = packet.name or f"Company {co_id}"
        self.companies[co_id] = {
            'id': co_id,
            'name': company_name,
            'founded': getattr(packet, 'year', 1950) or 1950
        }
        self.logger.info(f"Company {co_id}: {company_name}")
        await self._sync_pause_state()

    async def _on_company_remove(self, _admin, packet):
        co_id = self._co_id(packet.id)
        self.companies.pop(co_id, None)
        self.logger.info(f"Company {co_id} removed")
        await self._sync_pause_state()

    async def _on_company_economy(self, _admin, packet):
        co_id = self._co_id(packet.id)
        if co_id not in self.companies:
            self.companies[co_id] = {'id': co_id, 'name': f'Company {co_id}', 'founded': 1950}

        if getattr(packet, 'quarterly_info', None):
            self.companies[co_id]['value'] = packet.quarterly_info[-1]['company_value']
            self.logger.info(f"Company {co_id} value: {self.companies[co_id]['value']}")
            self._check_dead_companies()

    async def _on_client_info(self, _admin, packet):
        company_id = getattr(packet, 'play_as', getattr(packet, 'company_id', 255))
        cid = packet.id
        self.clients[cid] = {
            'id': cid,
            'name': packet.name,
            'company': company_id if company_id != 255 else 255
        }
        if cid in self.reset_pending and self.reset_pending[cid] != (company_id if company_id != 255 else 255):
            self.reset_pending.pop(cid, None)
        self.logger.info(f"Client {cid}: {packet.name} in company {company_id if company_id != 255 else 255}")
        await self._sync_pause_state()

    async def _wait_for_initial_data(self):
        """Wait for initial data to arrive via automatic subscriptions."""
        self.logger.info("Waiting for initial data via subscriptions...")
        
        max_wait = 10
        start_time = time.time()
        
        while time.time() - start_time < max_wait:
            try:
                packets = await self.admin.recv()
                for packet in packets:
                    await self.admin.handle_packet(packet)
                    self.data_received = True
            except:
                pass
            
            if self.game_date is not None or self.companies:
                self.logger.info(f"Initial data received - Date: {self.game_date}, Companies: {len(self.companies)}")
                break
                
            await asyncio.sleep(0.5)
        else:
            self.logger.warning("Timeout waiting for initial data, proceeding anyway")
        
        await self._sync_pause_state()

    async def _poll_manual(self, update_type: int, data: int):
        PACKET_TYPE = 3
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet_size = 3 + len(payload)
        packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
        if self.admin and self.admin._writer:
            self.admin._writer.write(packet)
            await self.admin._writer.drain()

    async def _poll_startup(self):
        self.logger.info("Polling startup data (date/clients/companies)")
        await self._poll_manual(openttdpacket.AdminUpdateType.DATE.value, 0)
        await self._poll_manual(openttdpacket.AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
        for company_id in range(16):
            await self._poll_manual(openttdpacket.AdminUpdateType.COMPANY_INFO.value, company_id)
            await self._poll_manual(openttdpacket.AdminUpdateType.COMPANY_ECONOMY.value, company_id)

    def _check_goal(self):
        """Placeholder goal check to avoid crash; implement logic if needed."""
        return

    def _check_dead_companies(self):
        """Check for dead companies and reset them immediately."""
        if not self.game_date or not self.companies:
            return
        
        current_year = self._get_current_year()
        
        for co_id, company_data in list(self.companies.items()):
            company_age = current_year - company_data.get('founded', 1950)
            company_value = company_data.get('value', 0)
            
            if company_age >= self.cfg.dead_co_age and company_value < self.cfg.dead_co_value:
                self.logger.info(f"Auto-cleaning Company {co_id}: age={company_age}y, value={self._fmt(company_value)}")
                
                for cid, client in list(self.clients.items()):
                    if client.get('company') == co_id:
                        asyncio.create_task(self.admin.send_rcon(f"move {cid} 255"))
                
                asyncio.create_task(self.admin.send_rcon(f"reset_company {co_id}"))
                self.companies.pop(co_id, None)
                
                asyncio.create_task(self._broadcast(f"Company {co_id} auto-reset for inactivity"))

    async def _sync_pause_state(self):
        """Ensure game pause state matches presence of companies."""
        active_players = [c for c in self.clients.values() if c.get('company') != 255]
        should_pause = len(self.companies) == 0 and len(active_players) == 0
        if should_pause == self.paused:
            return

        await self.admin.send_rcon("pause" if should_pause else "unpause")
        self.paused = should_pause
        self.logger.info(f"Game {'paused' if should_pause else 'unpaused'} (companies: {len(self.companies)}, players: {len(active_players)})")
    
    async def _handle_command(self, cid: int, cmd: str):
        parts = cmd.split()
        if not parts:
            return
        
        command = parts[0].lower()
        args = parts[1:]
        
        if command == 'help':
            await self._send_msg("Commands: !info, !rules, !cv, !reset", cid)
        elif command == 'info':
            info = [
                "=== Info ===",
                f"Goal: First company value {self._fmt(self.cfg.goal_value)} wins",
                "Gamescript: Production Booster on primary industries",
                "Transported >70% boosts production, <50% reduces",
            ]
            await self._send_msg('\n'.join(info), cid)
        elif command == 'rules':
            rules = [
                "=== Rules ===",
                "1. No griefing/sabotage",
                "2. No blocking players",
                "3. No cheating/exploits",
                "4. Be respectful",
                f"5. Inactive >{self.cfg.dead_co_age}y & <{self._fmt(self.cfg.dead_co_value)} auto-reset",
                "6. Admin decisions are final"
            ]
            await self._send_msg('\n'.join(rules), cid)
        elif command == 'cv':
            await self._show_company_values(cid)
        elif command == 'reset':
            await self._handle_reset_command(cid)
        elif command == 'yes':
            await self._confirm_reset(cid)
    
    async def _show_company_values(self, cid: int):
        if not self.companies:
            await self._send_msg("No companies", cid)
            return
        
        lines = ["=== Company Values ==="]
        for i, (co_id, data) in enumerate(sorted(self.companies.items(), key=lambda x: x[1]['value'], reverse=True)[:10], 1):
            name = data.get('name', f"Company {co_id}")
            value = self._fmt(data.get('value', 0))
            lines.append(f"{i}. {name}: {value}")
        
        await self._send_msg('\n'.join(lines), cid)
    
    async def _handle_reset_command(self, cid: int):
        client = await self._refresh_client(cid) or self.clients.get(cid)

        if not client or client.get('company') == 255:
            await self._send_msg("Must be in company to reset", cid)
            return

        co_id = client['company']
        self.reset_pending[cid] = co_id
        msg = f"=== Reset Company {co_id} ===\nThis DELETES your company!\nType !yes to confirm (10s timeout)"
        await self._send_msg(msg, cid)
        
        async def _expire():
            await asyncio.sleep(10)
            self.reset_pending.pop(cid, None)
        asyncio.create_task(_expire())

    async def _refresh_client(self, cid: int):
        return self.clients.get(cid)
    
    async def _confirm_reset(self, cid: int):
        if cid not in self.reset_pending:
            return
        
        co_id = self.reset_pending.pop(cid)

        client = await self._refresh_client(cid)
        if not client or client.get('company') not in (co_id, 255):
            await self._send_msg("Reset cancelled; your company changed.", cid)
            return

        await self.admin.send_rcon(f"move {cid} 255")

        moved = await self._ensure_spectator(cid)
        if not moved:
            await self._send_msg("Could not move you to spectators; reset cancelled.", cid)
            return

        await self.admin.send_rcon(f"reset_company {co_id}")
        self.companies.pop(co_id, None)
        await self._send_msg(f"Company {co_id} reset", cid)
        await self._sync_pause_state()

    async def _ensure_spectator(self, cid: int) -> bool:
        client = self.clients.get(cid)
        return client and client.get('company') == 255
    
    async def _send_msg(self, msg: str, cid: int = None):
        try:
            lines = msg.split('\n')
            for line in lines:
                if not line.strip():
                    continue
                    
                if cid:
                    self.logger.info(f"[ADMIN] To Client {cid}: {line}")
                    await self.admin._chat(line, action=openttdpacket.Actions.SERVER_MESSAGE,
                                   desttype=openttdpacket.ChatDestTypes.CLIENT, id=cid)
                else:
                    self.logger.info(f"[ADMIN] Broadcast: {line}")
                    await self.admin._chat(line, action=openttdpacket.Actions.SERVER_MESSAGE,
                                   desttype=openttdpacket.ChatDestTypes.BROADCAST)
                
                await asyncio.sleep(0.1)
        except:
            pass
    
    async def _broadcast(self, msg: str):
        await self._send_msg(msg)
    
    def _get_current_year(self) -> int:
        if self.game_date is not None:
            return (date(1, 1, 1) + timedelta(days=self.game_date)).year - 1
        return 1950

    def _fmt_date(self, day_count: int) -> str:
        d = date(1, 1, 1) + timedelta(days=day_count - 1)
        return d.strftime("%m-%d-%Y")
    
    def _fmt(self, val: int) -> str:
        if val >= 1_000_000_000:
            return f"{val / 1_000_000_000:.1f}b"
        if val >= 1_000_000:
            return f"{val / 1_000_000:.1f}m"
        if val >= 1_000:
            return f"{val / 1_000:.1f}k"
        return str(val)

def load_settings(path: str = "settings.json") -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        root_logger.error("settings.json not found at %s", path)
        raise
    except json.JSONDecodeError as e:
        root_logger.error("settings.json is invalid JSON: %s", e)
        raise

async def load_settings_async(path: str = "settings.json") -> dict:
    return await asyncio.to_thread(load_settings, path)

def build_server_configs(settings: dict) -> list:
    admin_ports = settings.get("admin_ports") or []
    configs = []
    for admin_port in admin_ports:
        cfg = Config(
            admin_port=admin_port,
            server_ip=settings.get("server_ip"),
            admin_name=settings.get("admin_name"),
            admin_pass=settings.get("admin_pass"),
            goal_value=settings.get("goal_value"),
            load_scenario=settings.get("load_scenario"),
            dead_co_age=settings.get("dead_co_age"),
            dead_co_value=settings.get("dead_co_value"),
            reset_countdown_seconds=settings.get("reset_countdown_seconds"),
        )
        configs.append(cfg)
    return configs

def signal_handler(signum: int, frame) -> None:
    root_logger.info("Received signal %d, shutting down...", signum)
    for bot in _running_bots:
        bot.stop_event.set()
    sys.exit(0)

_running_bots = []

async def main() -> None:
    settings = await load_settings_async()
    servers = build_server_configs(settings)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    root_logger.info('=== OpenTTD Admin Bot Starting ===')
    
    tasks = []
    for cfg in servers:
        bot = AsyncRconBot(cfg)
        _running_bots.append(bot)
        tasks.append(asyncio.create_task(bot.start()))
    
    await asyncio.gather(*tasks)

if __name__ == '__main__':
    asyncio.run(main())
