import logging
import re
import time
import threading
import concurrent.futures
import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass
from enum import Enum

from pyopenttdadmin import Admin, AdminUpdateType, AdminUpdateFrequency, openttdpacket

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("OpenTTDBot")


@dataclass
class Config:
    SERVER_IP: str = "192.168.1.10"
    SERVER_PORT: int = 3977
    ADMIN_NAME: str = "admin"
    ADMIN_PASS: str = "PASSWORDPASSWORD"
    GOAL_VALUE: int = 10_000_000_000
    LOAD_SCENARIO: str = "flat2048prodboost.scn"
    DEBUG: bool = True
    DEAD_CO_AGE: int = 5
    DEAD_CO_VALUE: int = 5_000_000


cfg = Config()
logger.setLevel(logging.DEBUG if cfg.DEBUG else logging.INFO)

CACHE_TTL: float = 5.0
RCON_REFRESH_TTL: float = 5.0
RCON_TIMEOUT: float = 5.0
RCON_GRACE: float = 0.15
MONITOR_INTERVAL_DEFAULT: int = 1800
MONITOR_INTERVAL_90PCT: int = 600
MONITOR_INTERVAL_95PCT: int = 180
MONITOR_INTERVAL_NO_CO: int = 300
PAUSE_DELAY: int = 5
GREETING_DELAY: int = 3
RESET_TIMEOUT: float = 30.0
MSG_RATE_LIMIT: float = 0.05
MAX_WORKERS: int = 6
DATE_POLL_INTERVAL: float = 1.0
DATE_STALL_THRESHOLD: float = 5.0

COMPANY_RE = re.compile(
    r"#\s*:?(\d+)(?:\([^)]+\))?\s+Company Name:\s*'([^']*)'\s+"
    r"Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+"
    r"Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I
)
CLIENT_RE = re.compile(r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)", re.I)
GETDATE_RE = re.compile(r"Date:\s*(\d{4}-\d{2}-\d{2})")


class GamePhase(Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    GOAL_REACHED = "goal_reached"
    RESETTING = "resetting"


class SafeAdmin(Admin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._send_lock = threading.Lock()
        self.last_raw_packet = None

    def _send(self, packet):
        with self._send_lock:
            super()._send(packet)

    def _recv(self, size: int):
        data = super()._recv(size)
        self.last_raw_packet = data
        return data


class OpenTTDBot:
    def __init__(self):
        self.admin = SafeAdmin(ip=cfg.SERVER_IP, port=cfg.SERVER_PORT)
        self.stop_event = threading.Event()
        self.main_thread = threading.current_thread()
        
        self.rcon_lock = threading.RLock()
        self.rcon_cv = threading.Condition(self.rcon_lock)
        self.rcon_buffer = []
        self.rcon_inflight = False
        self.rcon_end_ts = None
        self.last_companies_rcon = 0.0
        self.last_clients_rcon = 0.0
        
        self.game_state_cache = {'companies': {}, 'clients': {}}
        self.cache_ts = 0.0
        self.cache_lock = threading.RLock()
        self.state_initialized = False

        self.last_date = None
        self.last_date_year = None
        self.date_rcon_available = True
        self.last_date_days = None
        self.last_date_wall_ts = 0.0
        
        self.phase = GamePhase.WAITING
        self.phase_lock = threading.Lock()
        self.paused: Optional[bool] = None
        
        self.pause_timer = None
        self.pause_timer_lock = threading.Lock()
        
        self.reset_lock = threading.Lock()
        self.reset_pending = {}
        self.reset_timers = {}

        self.date_base: Optional[datetime.date] = None
        self.company_refresh_thread = None
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.pause_check_timer = None
        self.pause_check_lock = threading.Lock()
        
        self.commands = {
            'help': self.cmd_help,
            'info': self.cmd_info,
            'rules': self.cmd_rules,
            'cv': self.cmd_cv,
            'reset': self.cmd_reset,
            'yes': self.cmd_yes,
        }
        
        logger.info("Bot init: server=%s:%d", cfg.SERVER_IP, cfg.SERVER_PORT)

    def start(self):
        self.admin.add_handler(openttdpacket.ChatPacket)(self.on_chat)
        self.admin.add_handler(openttdpacket.ClientJoinPacket)(self.on_join)
        self.admin.add_handler(openttdpacket.ClientInfoPacket)(self.on_client_info)
        self.admin.add_handler(openttdpacket.ClientUpdatePacket)(self.on_update)
        self.admin.add_handler(openttdpacket.ClientQuitPacket)(self.on_client_quit)
        self.admin.add_handler(openttdpacket.RconPacket)(self.on_rcon)
        self.admin.add_handler(openttdpacket.RconEndPacket)(self.on_rcon_end)
        self.admin.add_handler(openttdpacket.NewGamePacket)(self.on_new_game)
        self.admin.add_handler(openttdpacket.WelcomePacket)(self.on_welcome)
        self.admin.add_handler(openttdpacket.DatePacket)(self.on_date)

        try:
            self.admin.add_handler(openttdpacket.CompanyNewPacket)(self.on_company_new)
            self.admin.add_handler(openttdpacket.CompanyUpdatePacket)(self.on_company_update)
            self.admin.add_handler(openttdpacket.CompanyRemovePacket)(self.on_company_remove)
            self.admin.add_handler(openttdpacket.CompanyInfoPacket)(self.on_company_info)
            self.admin.add_handler(openttdpacket.CompanyEconomyPacket)(self.on_company_economy)
        except AttributeError as e:
            logger.warning("Some company packet handlers not available: %s", e)

        try:
            logger.info("Connecting: %s:%d", cfg.SERVER_IP, cfg.SERVER_PORT)
            self.admin.login(cfg.ADMIN_NAME, cfg.ADMIN_PASS)
            logger.info("Logged in: %s", cfg.ADMIN_NAME)
            
            self.admin.subscribe(AdminUpdateType.CHAT)
            self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY)
            self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.POLL)
            self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
            
            try:
                self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.POLL)
                self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
                self.admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.POLL)
                self.admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.WEEKLY)
                logger.info("Subscribed to company updates")
            except (AttributeError, Exception) as e:
                logger.warning("Could not subscribe to company updates: %s", e)
            
            time.sleep(0.5)

            self._update_companies_from_rcon(reason="startup")

            out = self.rcon('clients')
            if out.strip():
                for line in out.splitlines():
                    m = CLIENT_RE.match(line)
                    if m:
                        try:
                            parsed_cid, parsed_name, co = m.groups()
                            cid = int(parsed_cid)
                            name = parsed_name.strip()
                            company_id = int(co) if co != '255' else 255
                            with self.cache_lock:
                                self.game_state_cache['clients'][cid] = {
                                    'name': name,
                                    'company': company_id,
                                }
                            logger.debug("Loaded client from RCON: %s", name)
                        except (ValueError, AttributeError):
                            pass

            with self.cache_lock:
                self.state_initialized = True

            self._cleanup_unnamed_company()
            
            try:
                self._monitor_tick()
            except Exception as e:
                logger.error("Initial monitor tick error: %s", e)

            threading.Thread(target=self._game_monitor_loop, daemon=True, name="GameMonitor").start()
            logger.info("Monitor loop started")
            
            self.broadcast("Admin connected")
            logger.info("Main loop start")

            if hasattr(self.admin, "poll"):
                threading.Thread(target=self._date_poll_loop, daemon=True, name="DatePoll").start()

            self.company_refresh_thread = threading.Thread(
                target=self._company_refresh_loop,
                daemon=True,
                name="CompanyRefresh"
            )
            self.company_refresh_thread.start()

            while not self.stop_event.is_set():
                try:
                    for p in self.admin.recv():
                        if isinstance(p, openttdpacket.DatePacket):
                            formatted_date = self._format_game_date(getattr(p, 'date', None))
                            logger.debug("Date %s", formatted_date)
                        self.admin.handle_packet(p)
                except ConnectionError as e:
                    logger.error("Connection lost: %s", e)
                    break
                except Exception as e:
                    logger.warning("Recv error: %s", e)
                self.stop_event.wait(0.1)
                
        except KeyboardInterrupt:
            logger.info("Shutdown: user interrupt")
        except Exception as e:
            logger.error("Fatal: %s", e, exc_info=True)
        finally:
            self._cleanup()

    def _cleanup(self):
        logger.info("Cleanup start")
        self.stop_event.set()
        
        with self.pause_timer_lock:
            if self.pause_timer:
                self.pause_timer.cancel()
                self.pause_timer = None
        
        with self.reset_lock:
            for timer in list(self.reset_timers.values()):
                timer.cancel()
            self.reset_timers.clear()
            self.reset_pending.clear()
        
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            self.executor.shutdown(wait=False)
        except Exception as e:
            logger.debug("Executor shutdown error: %s", e)
        
        logger.info("Cleanup done")

    def on_chat(self, admin, pkt: openttdpacket.ChatPacket):
        msg = pkt.message.strip()
        cid = getattr(pkt, 'id', None)
        
        if msg.startswith('!'):
            logger.info("CMD: c=%s msg=%s", cid, msg)
            self.executor.submit(self._process_cmd, cid, msg)

    def on_join(self, admin, pkt: openttdpacket.ClientJoinPacket):
        cid = getattr(pkt, 'id', None)
        logger.info("Join: c=%s", cid)
        if cid:
            self.executor.submit(self._cleanup_unnamed_company)
            self.executor.submit(self._greet, cid)

    def on_client_info(self, admin, pkt: openttdpacket.ClientInfoPacket):
        cid = getattr(pkt, 'id', None)
        if cid is None:
            return
        with self.cache_lock:
            self.game_state_cache['clients'][cid] = {
                'name': getattr(pkt, 'name', ''),
                'company': getattr(pkt, 'company_id', 255),
            }
            self.cache_ts = time.time()
            self.state_initialized = True
        self.executor.submit(self._check_pause_state)

    def on_client_quit(self, admin, pkt: openttdpacket.ClientQuitPacket):
        cid = getattr(pkt, 'id', None)
        if cid is None:
            return
        with self.reset_lock:
            if cid in self.reset_pending:
                logger.info("Client %d quit with pending reset, cancelling", cid)
                self.reset_pending.pop(cid, None)
                if cid in self.reset_timers:
                    self.reset_timers[cid].cancel()
                    self.reset_timers.pop(cid, None)
        with self.cache_lock:
            self.game_state_cache['clients'].pop(cid, None)
            self.cache_ts = time.time()

    def on_update(self, admin, pkt: openttdpacket.ClientUpdatePacket):
        cid = getattr(pkt, 'id', None)
        if not cid:
            return
            
        co = getattr(pkt, 'company_id', None)
        if co is None:
            co = getattr(pkt, 'company', None)
        elif co == 0:
            co = 1
        name = getattr(pkt, 'name', None)
        
        co_id = None
        if co is not None:
            try:
                co_id = int(co)
            except (ValueError, TypeError):
                co_id = co
        
        with self.cache_lock:
            clients = self.game_state_cache['clients']
            cached = clients.get(cid, {})
            clients[cid] = {
                'name': name or cached.get('name', ''),
                'company': co_id if co_id is not None else cached.get('company')
            }
            self._invalidate_cache()
        
        if cached.get('company') != co_id:
            with self.reset_lock:
                if cid in self.reset_pending:
                    pending_co = self.reset_pending[cid]
                    logger.info(
                        "Client %d switched from company %s to %s, cancelling pending reset for company %s",
                        cid, cached.get('company'), co_id, pending_co
                    )
                    self.reset_pending.pop(cid, None)
                    if cid in self.reset_timers:
                        self.reset_timers[cid].cancel()
                        del self.reset_timers[cid]
                    try:
                        self.send_msg("Reset cancelled: you switched companies", cid)
                    except Exception:
                        pass
        
        logger.debug("Update: c=%s co=%s n=%s", cid, co_id, name)
        self.executor.submit(self._check_pause_state)

    def on_company_new(self, admin, pkt):
        co_id = getattr(pkt, 'id', None)
        if co_id is None:
            co_id = getattr(pkt, 'company_id', None)
        elif co_id == 0:
            co_id = 1
        logger.info("Company created: co=%s", co_id)
        with self.cache_lock:
            self._invalidate_cache()
        self.executor.submit(self._check_pause_state)

    def on_company_info(self, admin, pkt):
        co_id = getattr(pkt, 'id', None)
        if co_id is None:
            co_id = getattr(pkt, 'company_id', None)
        elif co_id == 0:
            co_id = 1
        if co_id is None:
            return
        with self.cache_lock:
            existing = self.game_state_cache['companies'].get(co_id, {})
            self.game_state_cache['companies'][co_id] = {
                'display_id': co_id,
                'name': getattr(pkt, 'name', existing.get('name', '')),
                'start_date': getattr(pkt, 'start_date', existing.get('start_date')),
                'money': existing.get('money', 0),
                'loan': existing.get('loan', 0),
                'value': existing.get('value', 0),
            }
            self.cache_ts = time.time()
            self.state_initialized = True
        self.executor.submit(self._check_pause_state)

    def on_company_update(self, admin, pkt):
        co_id = getattr(pkt, 'id', None)
        if co_id is None:
            co_id = getattr(pkt, 'company_id', None)
        elif co_id == 0:
            co_id = 1
        logger.debug("Company updated: co=%s", co_id)
        with self.cache_lock:
            existing = self.game_state_cache['companies'].get(co_id, {})
            self.game_state_cache['companies'][co_id] = {
                'display_id': co_id,
                'name': getattr(pkt, 'name', existing.get('name', '')),
                'start_date': existing.get('start_date'),
                'money': existing.get('money', 0),
                'loan': existing.get('loan', 0),
                'value': existing.get('value', 0),
            }
            self.cache_ts = time.time()

        self._schedule_pause_check()

    def on_company_remove(self, admin, pkt):
        co_id = getattr(pkt, 'id', None)
        fallback = getattr(pkt, 'company_id', None)
        if co_id is None:
            co_id = fallback
        elif co_id == 0:
            co_id = fallback if fallback not in (None, 0) else 1
        logger.info("Company removed: co=%s", co_id)
        with self.reset_lock:
            for cid, pending_co in list(self.reset_pending.items()):
                if pending_co == co_id:
                    logger.info("Company %d removed, cancelling pending reset from client %d", co_id, cid)
                    self.reset_pending.pop(cid, None)
                    if cid in self.reset_timers:
                        self.reset_timers[cid].cancel()
                        self.reset_timers.pop(cid, None)
                    try:
                        self.send_msg(f"Reset cancelled: company {co_id} was removed", cid)
                    except Exception:
                        pass
        with self.cache_lock:
            self.game_state_cache['companies'].pop(co_id, None)
            self._invalidate_cache()
        self._schedule_pause_check()

    def on_company_economy(self, admin, pkt: openttdpacket.CompanyEconomyPacket):
        co_id = getattr(pkt, 'id', None)
        if co_id is None:
            return
        if co_id == 0:
            co_id = 1
        changed = False
        with self.cache_lock:
            existing = self.game_state_cache['companies'].get(co_id, {})
            money = getattr(pkt, 'money', existing.get('money', 0))
            loan = getattr(pkt, 'current_loan', existing.get('loan', 0))
            value = getattr(pkt, 'company_value', existing.get('value', 0))
            changed = (
                money != existing.get('money')
                or loan != existing.get('loan')
                or value != existing.get('value')
            )
            if changed:
                self.game_state_cache['companies'][co_id] = {
                    'display_id': co_id,
                    'name': existing.get('name', ''),
                    'start_date': existing.get('start_date'),
                    'money': money,
                    'loan': loan,
                    'value': value,
                }
                self.cache_ts = time.time()
                self.state_initialized = True
        if changed:
            self._schedule_pause_check()
            self.executor.submit(self._check_goal_from_cache)

    def on_date(self, admin, pkt: openttdpacket.DatePacket):
        self.last_date = getattr(pkt, 'date', None)
        resolved = self._resolve_game_date(self.last_date)
        if resolved:
            self.last_date_year = resolved.year
        now = time.time()
        self.last_date_days = self.last_date
        self.last_date_wall_ts = now
        if self.paused:
            self.paused = False
            logger.info("Pause cleared: date advanced to %s", resolved.isoformat() if resolved else self.last_date)

    def on_rcon(self, admin, pkt: openttdpacket.RconPacket):
        text = getattr(pkt, "response", "").strip()
        if text:
            with self.rcon_lock:
                if self.rcon_inflight:
                    self.rcon_buffer.append(text)
                    self.rcon_cv.notify_all()

    def on_rcon_end(self, admin, pkt: openttdpacket.RconEndPacket):
        with self.rcon_lock:
            if self.rcon_inflight:
                self.rcon_end_ts = time.time()
                self.rcon_inflight = False
                logger.debug("RCON end: lines=%d", len(self.rcon_buffer))
                self.rcon_cv.notify_all()

    def on_new_game(self, admin, pkt):
        logger.info("New game/map detected")
        time.sleep(0.5)
        with self.cache_lock:
            self.game_state_cache['companies'].clear()
            self.game_state_cache['clients'].clear()
            self._invalidate_cache()
            self.state_initialized = False

        self._cleanup_unnamed_company()
        self._check_pause_state()

    def on_welcome(self, admin, pkt):
        logger.info("Welcome packet received")

        logger.info("Waiting 5 seconds before proceeding...")
        time.sleep(5)
    def rcon(self, cmd: str, timeout: float = None) -> str:
        timeout = timeout or RCON_TIMEOUT
        deadline = time.time() + timeout
        
        with self.rcon_lock:
            while self.rcon_inflight:
                if time.time() >= deadline:
                    logger.warning("RCON wait timeout: %s", cmd)
                    return ""
                self.rcon_cv.wait(0.2)

            self.rcon_buffer.clear()
            self.rcon_inflight = True
            self.rcon_end_ts = None
            self.admin.send_rcon(cmd.replace('"', '\\"'))
            logger.debug("RCON send: %s", cmd)

        if threading.current_thread() is self.main_thread:
            return self._rcon_recv_main(deadline, cmd)
        return self._rcon_recv_thread(deadline, cmd)

    def _rcon_recv_main(self, deadline: float, cmd: str) -> str:
        while time.time() < deadline:
            for p in self.admin.recv():
                self.admin.handle_packet(p)
            
            with self.rcon_lock:
                if not self.rcon_inflight and self.rcon_end_ts:
                    if time.time() - self.rcon_end_ts >= RCON_GRACE:
                        return '\n'.join(self.rcon_buffer)
            time.sleep(0.01)
        
        with self.rcon_lock:
            self.rcon_inflight = False
            self.rcon_cv.notify_all()
        logger.warning("RCON timeout: %s", cmd)
        return ""

    def _rcon_recv_thread(self, deadline: float, cmd: str) -> str:
        with self.rcon_lock:
            while time.time() < deadline:
                if not self.rcon_inflight and self.rcon_end_ts:
                    if time.time() - self.rcon_end_ts >= RCON_GRACE:
                        return '\n'.join(self.rcon_buffer)
                self.rcon_cv.wait(0.2)
            
            self.rcon_inflight = False
        logger.warning("RCON timeout: %s", cmd)
        return ""

    def _invalidate_cache(self):
        self.cache_ts = 0.0

    def _schedule_pause_check(self, delay: float = 0.5):
        with self.pause_check_lock:
            if self.pause_check_timer:
                self.pause_check_timer.cancel()
            self.pause_check_timer = threading.Timer(delay, self._run_pause_check)
            self.pause_check_timer.start()

    def _run_pause_check(self):
        with self.pause_check_lock:
            self.pause_check_timer = None
        self.executor.submit(self._check_pause_state)

    def _cache_valid(self) -> bool:
        return time.time() - self.cache_ts < CACHE_TTL

    def _refresh_game_state(self) -> Dict:
        need_refresh = False
        with self.cache_lock:
            if not self._cache_valid():
                self.cache_ts = time.time()
                need_refresh = True
            snapshot = dict(self.game_state_cache)
        # Bias to RCON when TTL expired even if cache still valid
        if not need_refresh:
            now = time.time()
            if (now - self.last_companies_rcon >= RCON_REFRESH_TTL) or (now - self.last_clients_rcon >= RCON_REFRESH_TTL):
                need_refresh = True
        if need_refresh:
            try:
                self._update_companies_from_rcon(reason="cache_refresh")
                self._refresh_client_companies()
            except Exception:
                logger.debug("Company refresh on cache refresh failed", exc_info=True)
            with self.cache_lock:
                snapshot = dict(self.game_state_cache)
        if not snapshot.get('companies'):
            has_client_company = any(
                c.get('company') not in (None, 255) for c in snapshot.get('clients', {}).values()
            )
            if has_client_company:
                try:
                    self._update_companies_from_rcon(reason="clients_hint")
                    self._refresh_client_companies()
                except Exception:
                    logger.debug("Company refresh from clients hint failed", exc_info=True)
                with self.cache_lock:
                    snapshot = dict(self.game_state_cache)
        return snapshot

    def _get_current_year(self, companies: Dict) -> float:
        out = self._rcon_date()
        if out:
            m = GETDATE_RE.search(out)
            if m:
                year_str = m.group(1)
                try:
                    parsed = datetime.datetime.strptime(year_str, "%Y-%m-%d").date()
                    return float(parsed.year)
                except ValueError:
                    return float(year_str.split('-')[0])
        if companies:
            max_year = max((co.get("start_date", 0) for co in companies.values()), default=1950)
            return float(max_year)
        return 1950.0

    def _resolve_game_date(self, packet_days: Optional[int]) -> Optional[datetime.date]:
        if packet_days is None:
            return None

    def _refresh_client_companies(self):
        """Refresh client->company associations from RCON output."""
        if time.time() - self.last_clients_rcon < RCON_REFRESH_TTL:
            return
        try:
            out = self.rcon('clients')
            if out.strip():
                for line in out.splitlines():
                    m = CLIENT_RE.match(line)
                    if not m:
                        continue
                    try:
                        parsed_cid, _, parsed_co = m.groups()
                        parsed_cid = int(parsed_cid)
                        company_id = int(parsed_co) if parsed_co != '255' else 255
                        with self.cache_lock:
                            if parsed_cid not in self.game_state_cache['clients']:
                                self.game_state_cache['clients'][parsed_cid] = {}
                            self.game_state_cache['clients'][parsed_cid]['company'] = company_id
                    except (ValueError, AttributeError):
                        continue
            self.last_clients_rcon = time.time()
        except Exception:
            logger.debug("Client refresh failed", exc_info=True)

    def _refresh_companies_and_clients(self, reason: str = "manual", force: bool = False):
        """Refresh authoritative companies and client->company mapping via RCON."""
        now = time.time()
        try:
            if force or (now - self.last_companies_rcon >= RCON_REFRESH_TTL):
                self._update_companies_from_rcon(reason=reason)
        except Exception:
            logger.debug("Company refresh (%s) failed", reason, exc_info=True)
        try:
            if force or (now - self.last_clients_rcon >= RCON_REFRESH_TTL):
                self._refresh_client_companies()
        except Exception:
            logger.debug("Client refresh (%s) failed", reason, exc_info=True)

    def _format_game_date(self, packet_days: Optional[int]) -> str:
        resolved = self._resolve_game_date(packet_days)
        if resolved:
            return resolved.isoformat()
        if packet_days is None:
            return "unknown"
        return f"day {packet_days}"

    def _rcon_date(self) -> str:
        if not self.date_rcon_available:
            return ""
        try:
            out = self.rcon('date')
            if out and "command 'date' not found" in out.lower():
                self.date_rcon_available = False
                logger.debug("RCON 'date' unavailable; disabling further date commands")
                return ""
            return out if out else ""
        except Exception:
            self.date_rcon_available = False
            return ""

    def _ensure_paused(self) -> bool:
        try:
            self.rcon('pause')
            self.paused = True
            return True
        except Exception as e:
            logger.warning("Pause command failed: %s", e)
            return False

    def _date_poll_loop(self):
        if not hasattr(self.admin, "poll"):
            return
        while not self.stop_event.wait(DATE_POLL_INTERVAL):
            try:
                self.admin.poll(AdminUpdateType.DATE)
            except Exception as e:
                logger.debug("Date poll failed: %s", e)
            if self.last_date_wall_ts:
                stalled = time.time() - self.last_date_wall_ts
                if stalled > DATE_STALL_THRESHOLD and self.paused is not True:
                    self.paused = True
                    logger.info("Pause inferred: date stalled %.1fs", stalled)

    def _update_companies_from_rcon(self, reason: str = "") -> None:
        out = self.rcon('companies')
        if not out.strip():
            return

        updated = 0
        for line in out.splitlines():
            m = COMPANY_RE.match(line)
            if not m:
                continue
            try:
                parsed_id, parsed_name, year, money, loan, value = m.groups()
                co_id = int(parsed_id)
                co_name = parsed_name.strip()
                start_date = int(year)
                money_val = int(money.replace(',', ''))
                loan_val = int(loan.replace(',', ''))
                value_val = int(value.replace(',', ''))
                with self.cache_lock:
                    self.game_state_cache['companies'][co_id] = {
                        'display_id': co_id,
                        'name': co_name,
                        'start_date': start_date,
                        'money': money_val,
                        'loan': loan_val,
                        'value': value_val,
                    }
                updated += 1
            except (ValueError, AttributeError):
                continue

        if updated:
            with self.cache_lock:
                self.cache_ts = time.time()
                self.state_initialized = True
            self.last_companies_rcon = time.time()
            if reason:
                logger.info("Companies refreshed via RCON: %s (updated=%d)", reason, updated)

    def _company_refresh_loop(self):
        while not self.stop_event.wait(60):
            if self.paused is True:
                continue
            with self.phase_lock:
                phase = self.phase
            if phase != GamePhase.ACTIVE:
                continue
            try:
                self._update_companies_from_rcon(reason="periodic")
            except Exception:
                logger.warning("Periodic companies refresh failed", exc_info=True)

    def _cleanup_unnamed_company(self):
        try:
            companies = self._refresh_game_state().get('companies', {})
            
            if not companies:
                out = self.rcon('companies')
                if out.strip():
                    for line in out.splitlines():
                        m = COMPANY_RE.match(line)
                        if not m:
                            continue
                        try:
                            parsed_id, parsed_name, *_ = m.groups()
                            co_id = int(parsed_id)
                            co_name = parsed_name.strip()
                            if co_name.lower() == 'unnamed':
                                logger.info("Deleting unnamed company via RCON: id=%s", co_id)
                                self.rcon(f"reset_company {co_id}")
                                time.sleep(0.1)
                                with self.cache_lock:
                                    self._invalidate_cache()
                                return True
                        except (ValueError, AttributeError):
                            continue
                return False
            
            for co_id, data in companies.items():
                co_name = (data.get('name') or '').strip()
                if co_name.lower() == 'unnamed':
                    logger.info("Deleting unnamed company: id=%s", co_id)
                    self.rcon(f"reset_company {co_id}")
                    time.sleep(0.1)
                    with self.cache_lock:
                        self._invalidate_cache()
                    return True
            
            return False
        except Exception:
            logger.exception("Unnamed company cleanup failed")
            return False

    def _get_active_companies(self, companies: Dict, clients: Dict) -> Dict:
        active = {}
        for co_id, co_data in companies.items():
            co_name = (co_data.get('name') or '').strip()
            has_players = any(c.get('company') == co_id for c in clients.values())
            if co_name.lower() == 'unnamed' and not has_players:
                continue
            has_value = co_data.get('value', 0) > 0
            if has_players or has_value:
                active[co_id] = co_data
        return active
    
    def _check_pause_state(self):
        state = self._refresh_game_state()
        companies = state['companies']
        clients = state['clients']

        if not self.state_initialized and not companies and not clients:
            return
        
        active_companies = self._get_active_companies(companies, clients)
        should_pause = len(active_companies) == 0
        
        logger.debug("Pause check: active=%d should_pause=%s paused=%s", 
                    len(active_companies), should_pause, self.paused)
        
        needs_unpause = False
        with self.pause_timer_lock:
            if should_pause:
                if self.pause_timer is None and self.paused is not True:
                    self.pause_timer = threading.Timer(PAUSE_DELAY, self._do_pause)
                    self.pause_timer.start()
            else:
                if self.pause_timer:
                    self.pause_timer.cancel()
                    self.pause_timer = None
                if self.paused is not False:
                    needs_unpause = True

        if needs_unpause:
            self.rcon('unpause')
            self.paused = False
            logger.info("Game unpaused: %d active companies", len(active_companies))

    def _do_pause(self):
        with self.pause_timer_lock:
            self.pause_timer = None
            
        if not self.paused:
            if self._ensure_paused():
                logger.info("Game paused: no active companies")

    def _load_scenario(self) -> bool:
        logger.info("Loading: %s", cfg.LOAD_SCENARIO)
        resp = self.rcon(f"load_scenario {cfg.LOAD_SCENARIO}")
        
        if resp and "cannot be found" in resp.lower():
            logger.error("Scenario not found: %s", cfg.LOAD_SCENARIO)
            return False
        
        with self.cache_lock:
            self._invalidate_cache()
        
        logger.info("Scenario loaded: %s", cfg.LOAD_SCENARIO)
        time.sleep(0.5)
        self._cleanup_unnamed_company()
        return True

    def send_msg(self, msg: str, cid: Optional[int] = None):
        for line in msg.split('\n'):
            try:
                if cid:
                    self.admin._chat(line, action=openttdpacket.Actions.SERVER_MESSAGE,
                                   desttype=openttdpacket.ChatDestTypes.CLIENT, id=cid)
                else:
                    self.admin._chat(line, action=openttdpacket.Actions.SERVER_MESSAGE,
                                   desttype=openttdpacket.ChatDestTypes.BROADCAST)
            except Exception as e:
                logger.error("Send failed: %s", e)
            time.sleep(MSG_RATE_LIMIT)

    def broadcast(self, msg: str):
        self.send_msg(msg)

    def _game_monitor_loop(self):
        logger.info("Monitor loop start")
        while not self.stop_event.is_set():
            interval = self._monitor_tick()
            self.stop_event.wait(interval)
        logger.info("Monitor loop stop")

    def _monitor_tick(self) -> int:
        try:
            self._refresh_companies_and_clients(reason="monitor_tick", force=True)
            self._cleanup_unnamed_company()
            
            state = self._refresh_game_state()
            companies = state['companies']
            clients = state['clients']
            
            active_companies = self._get_active_companies(companies, clients)

            if not active_companies:
                self._check_pause_state()
                return MONITOR_INTERVAL_NO_CO

            interval = self._check_goal(active_companies)
            self._check_dead_companies(active_companies, clients)
            self._check_pause_state()
            return interval

        except Exception as e:
            logger.error("Monitor error: %s", e)
            return MONITOR_INTERVAL_DEFAULT

    def _check_goal(self, companies: Dict) -> int:
        if not companies:
            return MONITOR_INTERVAL_NO_CO
            
        top_id, top = max(companies.items(), key=lambda x: x[1]['value'])
        ratio = top['value'] / cfg.GOAL_VALUE
        
        logger.debug("Top: %s=$%s (%.1f%%)", top['name'], self._fmt(top['value']), ratio * 100)
        
        if top['value'] >= cfg.GOAL_VALUE:
            with self.phase_lock:
                if self.phase in (GamePhase.GOAL_REACHED, GamePhase.RESETTING):
                    return MONITOR_INTERVAL_DEFAULT
                logger.info("GOAL! %s=$%s", top['name'], self._fmt(top['value']))
                self.phase = GamePhase.GOAL_REACHED
            self._handle_goal(top['name'], top['value'])
            return MONITOR_INTERVAL_DEFAULT
        
        if ratio >= 0.95:
            return MONITOR_INTERVAL_95PCT
        elif ratio >= 0.9:
            return MONITOR_INTERVAL_90PCT
        else:
            return MONITOR_INTERVAL_DEFAULT

    def _check_goal_from_cache(self):
        try:
            state = self._refresh_game_state()
            companies = state.get('companies', {})
            if not companies:
                return
            self._check_goal(companies)
        except Exception:
            logger.debug("Goal check from cache failed", exc_info=True)

    def _check_dead_companies(self, companies: Dict, clients: Dict):
        if not companies:
            return

        year = self._get_current_year(companies)

        for co_id, co_data in companies.items():
            founded = co_data.get('start_date')
            if not founded or founded <= 1950:
                continue

            age = year - founded
            value = co_data.get('value', 0)

            if age >= cfg.DEAD_CO_AGE and value < cfg.DEAD_CO_VALUE:
                self._cleanup_company(co_id, co_data, clients)

    def _cleanup_company(self, co_id: int, co_data: dict, clients: dict):
        name = co_data.get('name', f"Co {co_id}")
        co_clients = [c for c, d in clients.items() if d.get('company') == co_id]
        
        logger.info("Cleanup: co=%d n=%s cl=%d", co_id, name, len(co_clients))
        
        for cid in co_clients:
            self.rcon(f'move {cid} 255')
            time.sleep(0.02)
        
        self.rcon(f"reset_company {co_id}")
        with self.cache_lock:
            self._invalidate_cache()
        self.broadcast(f"Dead company cleanup: {name}")
        logger.info("Cleanup done: co=%d", co_id)

    def _handle_goal(self, winner: str, value: int):
        try:
            with self.phase_lock:
                if self.phase == GamePhase.RESETTING:
                    return
                self.phase = GamePhase.RESETTING

            safe_winner = winner or "Unknown"
            logger.info("Goal handler: winner=%s value=%s", safe_winner, self._fmt(value))
            msg = (
                "=== GOAL ACHIEVED ===\n"
                f"Winner: {safe_winner}\n"
                f"Goal: ${self._fmt(cfg.GOAL_VALUE)}\n"
                "Map restart in 20s..."
            )
            self.broadcast(msg)
            threading.Thread(target=self._reset_countdown, daemon=True).start()
        except Exception:
            logger.exception("Goal handler failed")

    def _reset_countdown(self):
        try:
            logger.info("Reset countdown: 20s")
            for i in range(20, 0, -1):
                if i in (10, 5):
                    self.broadcast(f"Map reset in {i}s...")
                if self.stop_event.wait(1):
                    logger.info("Reset countdown aborted")
                    return

            if self._load_scenario():
                self.broadcast("New map loaded")
                with self.phase_lock:
                    self.phase = GamePhase.WAITING
                self.paused = False
                self._check_pause_state()
            else:
                self.broadcast("Map reset failed!")
                with self.phase_lock:
                    self.phase = GamePhase.ACTIVE
        except Exception:
            logger.exception("Reset countdown failed")

    def _get_client_name(self, cid: int) -> str:
        with self.cache_lock:
            name = self.game_state_cache['clients'].get(cid, {}).get('name')
            if name:
                return name
        
        out = self.rcon('clients')
        if out.strip():
            for line in out.splitlines():
                m = CLIENT_RE.match(line)
                if not m:
                    continue
                try:
                    parsed_cid, parsed_name, co = m.groups()
                    if int(parsed_cid) == cid:
                        with self.cache_lock:
                            if cid not in self.game_state_cache['clients']:
                                self.game_state_cache['clients'][cid] = {}
                            self.game_state_cache['clients'][cid]['name'] = parsed_name
                        return parsed_name
                except (ValueError, AttributeError):
                    continue
        
        return f"C{cid}"

    def _get_client_company(self, cid: int) -> Optional[int]:
        with self.cache_lock:
            co = self.game_state_cache['clients'].get(cid, {}).get('company')
            if co is not None:
                return co
        
        out = self.rcon('clients')
        if out.strip():
            for line in out.splitlines():
                m = CLIENT_RE.match(line)
                if not m:
                    continue
                try:
                    parsed_cid, parsed_name, parsed_co = m.groups()
                    if int(parsed_cid) == cid:
                        company_id = int(parsed_co) if parsed_co != '255' else 255
                        with self.cache_lock:
                            if cid not in self.game_state_cache['clients']:
                                self.game_state_cache['clients'][cid] = {}
                            self.game_state_cache['clients'][cid]['company'] = company_id
                        return company_id
                except (ValueError, AttributeError):
                    continue
        
        return None
    
    def _greet(self, cid: int):
        if self.stop_event.wait(GREETING_DELAY):
            return
        name = self._get_client_name(cid)
        logger.info("Greeting c=%s n=%s", cid, name)
        self.send_msg(f"Welcome {name}! Type !help for commands", cid)

    def _process_cmd(self, cid: int, msg: str):
        parts = msg[1:].split()
        if not parts:
            return
        
        cmd = parts[0].lower()
        args = parts[1:]
        
        handler = self.commands.get(cmd)
        if not handler:
            return
        
        try:
            state = self._refresh_game_state()
            handler(cid, args, state)
        except Exception as e:
            logger.error("CMD error: cmd=%s c=%d e=%s", cmd, cid, e)
            self.send_msg("Command failed", cid)

    def cmd_help(self, cid: int, args: List[str], state: Dict):
        self.send_msg("Commands: !info, !rules, !cv, !reset", cid)

    def cmd_info(self, cid: int, args: List[str], state: Dict):
        info = [
            "=== Server Info ===",
            "South-East-Asia OpenTTD Server",
            "Gamescript: Production Booster on primary industries",
            "Transport >70% boosts production, <50% reduces",
            f"Goal: First company value ${self._fmt(cfg.GOAL_VALUE)} wins"
        ]
        self.send_msg('\n'.join(info), cid)

    def cmd_rules(self, cid: int, args: List[str], state: Dict):
        rules = [
            "=== Rules ===",
            "1. No griefing/sabotage",
            "2. No blocking players",
            "3. No cheating/exploits",
            "4. Be respectful",
            f"5. Inactive >{cfg.DEAD_CO_AGE}y & <${self._fmt(cfg.DEAD_CO_VALUE)} auto-reset",
            "6. Admin decisions final"
        ]
        self.send_msg('\n'.join(rules), cid)

    def cmd_cv(self, cid: int, args: List[str], state: Dict):
        # Always refresh via RCON to avoid stale rankings
        self._refresh_companies_and_clients(reason="cmd_cv_force", force=True)
        state = self._refresh_game_state()
        companies = state.get('companies', {})
        clients = state.get('clients', {})

        active_companies = self._get_active_companies(companies, clients)
        
        if not active_companies:
            self.send_msg("No companies", cid)
            return
        
        sorted_cos = sorted(active_companies.items(), key=lambda x: x[1]['value'], reverse=True)
        lines = ["=== Company Value Rankings ==="]
        
        for i, (co_id, data) in enumerate(sorted_cos[:10], 1):
            pct = (data['value'] / cfg.GOAL_VALUE) * 100
            lines.append(f"{i}. {data['name']} (#{data['display_id']}): ${self._fmt(data['value'])} ({pct:.1f}%)")
        
        self.send_msg('\n'.join(lines), cid)

    def cmd_reset(self, cid: int, args: List[str], state: Dict):
        # Refresh authoritative state before determining company to reset
        self._refresh_companies_and_clients(reason="cmd_reset", force=True)

        co_id = self._get_client_company(cid)
        if co_id is None or co_id == 255:
            self.send_msg("Must be in company to reset", cid)
            return
        
        with self.reset_lock:
            if cid in self.reset_timers:
                self.reset_timers[cid].cancel()
            
            self.reset_pending[cid] = co_id
        msg = f"=== Reset Company {co_id} ===\nThis DELETES your company!\nType !yes to confirm (30s timeout)"
        self.send_msg(msg, cid)
        
        timer = threading.Timer(RESET_TIMEOUT, self._cancel_reset, args=[cid])
        with self.reset_lock:
            self.reset_timers[cid] = timer
            timer.start()
        logger.info("Reset req: c=%d co=%d", cid, co_id)

    def _cancel_reset(self, cid: int):
        with self.reset_lock:
            self.reset_pending.pop(cid, None)
            timer = self.reset_timers.pop(cid, None)
        if timer:
            timer.cancel()

    def cmd_yes(self, cid: int, args: List[str], state: Dict):
        # Refresh authoritative state before confirming
        self._refresh_companies_and_clients(reason="cmd_yes_pre", force=True)

        with self.reset_lock:
            if cid not in self.reset_pending:
                self.send_msg("No pending reset. Use !reset first", cid)
                return
            
            pending_co_id = self.reset_pending.pop(cid)
            
            if cid in self.reset_timers:
                self.reset_timers[cid].cancel()
                del self.reset_timers[cid]
        
        current_co = self._get_client_company(cid)
        if current_co != pending_co_id:
            self.send_msg(
                f"Reset cancelled: you were in company {pending_co_id} but are now in company {current_co if current_co != 255 else 'spectator'}. "
                "Run !reset again from the company you want to delete.",
                cid
            )
            return

        try:
            self._update_companies_from_rcon(reason="reset_verify")
        except Exception:
            logger.debug("Company refresh before reset failed", exc_info=True)
        
        with self.cache_lock:
            company_exists = pending_co_id in self.game_state_cache.get('companies', {})
        
        if not company_exists:
            self.send_msg(f"Company {pending_co_id} no longer exists.", cid)
            return
        
        logger.info("Reset confirm: c=%d co=%d", cid, pending_co_id)
        self.rcon(f'move {cid} 255')
        time.sleep(0.2)
        
        self.rcon(f"reset_company {pending_co_id}")
        
        try:
            self._update_companies_from_rcon(reason="reset_confirm")
        except Exception:
            logger.debug("Company refresh after reset failed", exc_info=True)
        
        with self.cache_lock:
            self.game_state_cache['companies'].pop(pending_co_id, None)
            self._invalidate_cache()
        
        self.send_msg(f"Company {pending_co_id} reset", cid)
        logger.info("Reset done: co=%d", pending_co_id)
        self._check_pause_state()

    def _fmt(self, val: int) -> str:
        if val >= 1_000_000_000:
            return f"{val/1_000_000_000:.1f}B"
        if val >= 1_000_000:
            return f"{val/1_000_000:.1f}M"
        if val >= 1_000:
            return f"{val/1_000:.1f}k"
        return str(val)


if __name__ == "__main__":
    # meh, i hate people who sell free stuff, this is free! get at: https://github.com/nelbin4/openttd-admin-bot/
    logger.info("=== OpenTTD Admin Bot Starting ===")
    bot = OpenTTDBot()
    bot.start()
