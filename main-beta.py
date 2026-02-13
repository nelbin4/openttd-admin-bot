import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

from pyopenttdadmin import Admin, openttdpacket, AdminUpdateType, AdminUpdateFrequency
from pyopenttdadmin.enums import Actions, ChatDestTypes

COMPANY_RE = re.compile(
    r"#\s*:?(\d+)(?:\([^)]+\))?\s+Company Name:\s*'([^']*)'\s+"
    r"Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+"
    r"Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I)
CLIENT_RE = re.compile(r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)", re.I)
DATE_RE = re.compile(r"Date:\s*(\d{4})-\d{2}-\d{2}")


def fmt(v):
    if v >= 1_000_000_000: return f"{v/1e9:.1f}b"
    if v >= 1_000_000: return f"{v/1e6:.1f}m"
    if v >= 1_000: return f"{v/1e3:.1f}k"
    return str(v)


class Bot:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.admin = None
        self.running = False
        self.companies = {}
        self.clients = {}
        self.game_year = 0
        self.goal_reached = False
        self.cooldowns = {}
        self.reset_pending = {}
        self.paused = False

    def rcon(self, cmd):
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
        for line in text.split('\n'):
            if line.strip():
                if cid:
                    self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.CLIENT, cid)
                else:
                    self.admin._chat(line, Actions.SERVER_MESSAGE, ChatDestTypes.BROADCAST, 0)
                time.sleep(0.05)

    def poll_rcon(self):
        co_out = self.rcon("companies")
        cl_out = self.rcon("clients")
        dt_out = self.rcon("get_date")
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

    def build_cv(self):
        if not self.companies:
            return "No companies"
        lines = ["=== Company Values ==="]
        for i, (_, d) in enumerate(sorted(self.companies.items(), key=lambda x: x[1].get('value', 0), reverse=True)[:10], 1):
            lines.append(f"{i}. {d['name']}: {fmt(d.get('value', 0))}")
        return '\n'.join(lines)

    def auto_clean(self):
        for cid, data in list(self.companies.items()):
            age = self.game_year - data.get('founded', self.game_year)
            if age >= self.cfg['clean_age'] and data.get('value', 0) < self.cfg['clean_value']:
                for client_id, c in list(self.clients.items()):
                    if c['company_id'] == cid:
                        self.rcon(f"move {client_id} 255")
                self.rcon(f"reset_company {cid}")
                self.msg(f"Company {data['name']} auto-reset")
                self.log.info(f"Auto-clean: co#{cid} {data['name']} age={age} val={data.get('value',0)}")

    def check_goal(self):
        if self.goal_reached:
            return
        for cid, data in list(self.companies.items()):
            if data.get('value', 0) >= self.cfg['goal_value']:
                self.goal_reached = True
                self.log.info(f"Goal reached: {data['name']} with {fmt(data['value'])}")
                self.msg(f"{data['name']} WINS! Reached {fmt(self.cfg['goal_value'])} company value!")
                prev = 20
                for countdown in [20, 15, 10, 5]:
                    self.msg(f"Map resets in {countdown}s...")
                    time.sleep(prev - countdown if prev > countdown else 5)
                    prev = countdown
                time.sleep(5)
                self.rcon(f"load {self.cfg['load_map']}")
                self.companies.clear()
                self.clients.clear()
                self.game_year = 0
                self.goal_reached = False
                break

    def greet(self, client_id):
        time.sleep(5)
        if not self.running:
            return
        name = self.clients.get(client_id, {}).get('name', f'Player{client_id}')
        self.log.info(f"Greet: {name} (#{client_id})")
        self.msg(f"Welcome {name}! Type !help for commands")

    def handle_cmd(self, cid, text):
        if self.paused:
            return
        now = time.time()
        if now - self.cooldowns.get(cid, 0) < 3:
            return
        self.cooldowns[cid] = now
        cmd = text.split()[0].lower() if text.split() else ""
        self.log.info(f"Cmd: !{cmd} from #{cid}")
        if cmd == "help":
            self.msg("=== Commands ===\n!info !rules !cv !reset", cid)
        elif cmd == "info":
            self.msg(f"=== Game Info ===\nGoal: reach {fmt(self.cfg['goal_value'])} company value\nGamescript: Production Booster\nTransport >70% increases <50% decreases", cid)
        elif cmd == "rules":
            self.msg(f"=== Server Rules ===\n1. No griefing/sabotage\n2. No blocking players\n3. No cheating/exploits\n4. Be respectful\n5. Inactive cos (>={self.cfg['clean_age']}y & cv<{fmt(self.cfg['clean_value'])}) auto-reset", cid)
        elif cmd == "cv":
            self.poll_rcon()
            self.msg(self.build_cv(), cid)
        elif cmd == "reset":
            client = self.clients.get(cid)
            if not client or client.get('company_id') == 255:
                self.msg("Must be in a company to reset", cid)
                return
            co = client['company_id']
            if cid in self.reset_pending:
                self.msg("Reset already pending. Move to spectator to confirm", cid)
                return

            def expire():
                time.sleep(10)
                if cid in self.reset_pending:
                    self.reset_pending.pop(cid, None)
                    self.msg("Reset cancelled (timeout)", cid)

            self.reset_pending[cid] = co
            threading.Thread(target=expire, daemon=True).start()
            self.msg(f"Move to spectator within 10s to reset company #{co}", cid)

    def setup_handlers(self):
        @self.admin.add_handler(openttdpacket.ChatPacket)
        def on_chat(admin, pkt):
            msg = pkt.message.strip()
            self.log.debug(f"Chat: #{pkt.id} '{msg}'")
            if msg.startswith('!'):
                self.handle_cmd(pkt.id, msg[1:])

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        def on_client_info(admin, pkt):
            co = 255 if pkt.company_id == 255 else pkt.company_id + 1
            self.clients[pkt.id] = {'name': pkt.name, 'company_id': co}
            self.log.debug(f"ClientInfo: #{pkt.id} {pkt.name} co={co}")

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        def on_client_update(admin, pkt):
            co = 255 if pkt.company_id == 255 else pkt.company_id + 1
            if pkt.id in self.clients:
                self.clients[pkt.id]['company_id'] = co
            if pkt.id in self.reset_pending and co == 255:
                pending_co = self.reset_pending.pop(pkt.id)
                self.rcon(f"reset_company {pending_co}")
                self.msg(f"Company #{pending_co} reset", pkt.id)
                self.log.info(f"Reset confirmed: #{pkt.id} co={pending_co}")

        @self.admin.add_handler(openttdpacket.ClientJoinPacket)
        def on_client_join(admin, pkt):
            threading.Thread(target=self.greet, args=[pkt.id], daemon=True).start()

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        def on_client_remove(admin, pkt):
            self.clients.pop(pkt.id, None)
            self.reset_pending.pop(pkt.id, None)

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        def on_company_remove(admin, pkt):
            cid = 255 if pkt.id == 255 else pkt.id + 1
            self.companies.pop(cid, None)

        @self.admin.add_handler(openttdpacket.CompanyNewPacket)
        def on_company_new(admin, pkt):
            if self.paused:
                self.rcon("unpause")
                self.paused = False
                self.log.info("Unpaused: company created")

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        def on_new_game(admin, pkt):
            self.companies.clear()
            self.clients.clear()
            self.game_year = 0
            self.goal_reached = False
            self.reset_pending.clear()
            self.poll_rcon()
            self.log.info("New game detected, state reset")

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        def on_shutdown(admin, pkt):
            self.running = False
            self.log.info("Server shutdown received")

    def run(self):
        self.admin = Admin(ip=self.cfg['server_ip'], port=self.cfg['admin_port'])
        self.admin.login(self.cfg['admin_name'], self.cfg['admin_pass'])
        self.admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
        self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        self.admin.subscribe(AdminUpdateType.CONSOLE, AdminUpdateFrequency.AUTOMATIC)
        self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
        self.setup_handlers()
        self.poll_rcon()
        try:
            d1 = self.rcon("get_date")
            time.sleep(3)
            d2 = self.rcon("get_date")
            self.paused = DATE_RE.search(d1) and DATE_RE.search(d1).group(0) == (DATE_RE.search(d2).group(0) if DATE_RE.search(d2) else None)
        except Exception:
            self.paused = False
        self.running = True
        self.log.info(f"Connected to {self.cfg['server_ip']}:{self.cfg['admin_port']}")

        next_tick = time.time() // 60 * 60 + 60
        now_dt = datetime.now()
        next_hourly = (now_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()

        while self.running:
            for pkt in self.admin.recv():
                self.admin.handle_packet(pkt)
            if self.paused:
                time.sleep(0.2)
                continue
            now = time.time()
            if now >= next_tick:
                self.poll_rcon()
                if not self.companies and not self.paused:
                    self.rcon("pause")
                    self.paused = True
                    self.log.info("Paused: no companies")
                else:
                    self.auto_clean()
                    self.check_goal()
                next_tick = time.time() // 60 * 60 + 60
            if now >= next_hourly:
                self.poll_rcon()
                self.msg(self.build_cv())
                self.log.info("Hourly CV broadcast")
                next_hourly += 3600

        self.log.info("Bot stopped")


def run_bot(cfg, log):
    while True:
        try:
            Bot(cfg, log).run()
            break
        except Exception as e:
            log.error(f"Error: {e}, reconnecting in 10s...")
            time.sleep(10)


def main():
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    settings = json.load(open("settings.json"))
    logging.basicConfig(
        level=logging.DEBUG if settings.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("Bot")
    log.info("=== OpenTTD Admin Bot Starting ===")
    threads = []
    for port in settings["admin_ports"]:
        cfg = {**settings, "admin_port": port}
        name = f"[Server:{port}]"
        t = threading.Thread(target=run_bot, args=(cfg, logging.getLogger(name)), daemon=True, name=name)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
