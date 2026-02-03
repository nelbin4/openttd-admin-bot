# this is for 10 servers to admin. rename main10.py to main.py and include settings.json(adjust settings there)
import json
import logging
import re
import time
import threading
import concurrent.futures
import datetime
import signal
import sys
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass
from enum import Enum
from collections import OrderedDict
import tracemalloc

from pyopenttdadmin import Admin, openttdpacket, AdminUpdateType, AdminUpdateFrequency

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
root_logger = logging.getLogger("OpenTTDBot")

@dataclass
class Config:
    admin_port: int
    game_port: int
    server_num: int
    server_ip = ""
    admin_name = ""
    admin_pass = ""
    goal_value = 100_000_000_000
    load_scenario = "flat2048prodboost.scn"
    dead_co_age = 5
    dead_co_value = 5_000_000
    rcon_retry_max = 3
    rcon_retry_delay = 0.5
    reconnect_max_attempts = 10
    reconnect_delay = 5.0


GAME_START_YEAR = 1950
RESET_COUNTDOWN_SECONDS = 20


def load_settings(path="settings.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        root_logger.error("settings.json not found at %s", path)
        raise
    except json.JSONDecodeError as e:
        root_logger.error("settings.json is invalid JSON: %s", e)
        raise


def build_server_configs(settings):
    admin_ports = settings.get("admin_ports") or []
    game_ports = settings.get("game_ports") or []
    if game_ports and len(game_ports) != len(admin_ports):
        raise ValueError("game_ports length must match admin_ports when provided")

    configs = []
    for idx, admin_port in enumerate(admin_ports):
        game_port = game_ports[idx] if game_ports else 0
        cfg = Config(admin_port=admin_port, game_port=game_port, server_num=idx + 1)
        cfg.server_ip = settings.get("server_ip", cfg.server_ip)
        cfg.admin_name = settings.get("admin_name", cfg.admin_name)
        cfg.admin_pass = settings.get("admin_pass", cfg.admin_pass)
        cfg.goal_value = settings.get("goal_value", cfg.goal_value)
        cfg.load_scenario = settings.get("load_scenario", cfg.load_scenario)
        cfg.dead_co_age = settings.get("dead_co_age", cfg.dead_co_age)
        cfg.dead_co_value = settings.get("dead_co_value", cfg.dead_co_value)
        configs.append(cfg)
    return configs


SETTINGS = load_settings()
SERVERS = build_server_configs(SETTINGS)

CACHE_TTL = 30.0
CACHE_MAX_SIZE = 500
RCON_REFRESH_TTL = 10.0
RCON_TIMEOUT = 5.0
RCON_GRACE = 0.15
RCON_BUFFER_MAX = 1000
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT = 30.0
MONITOR_INTERVAL_DEFAULT = 1800
MONITOR_INTERVAL_90PCT = 600
MONITOR_INTERVAL_95PCT = 180
MONITOR_INTERVAL_NO_CO = 300
PAUSE_DELAY = 5
PAUSE_DEBOUNCE = 0.5
GREETING_DELAY = 3
RESET_TIMEOUT = 30.0
MSG_RATE_LIMIT = 0.05
COMMAND_COOLDOWN = 0.5
CLEANUP_INTERVAL = 300
MEMORY_CHECK_INTERVAL = 600
EXECUTOR_SHUTDOWN_TIMEOUT = 10.0
CLEANUP_SEMAPHORE_MAX = 3

COMPANY_RE = re.compile(
    r"#\s*:?(\d+)(?:\([^)]+\))?\s+Company Name:\s*'([^']*)'\s+"
    r"Year Founded:\s*(\d+)\s+Money:\s*\$?([-0-9,]+)\s+"
    r"Loan:\s*\$?(\d+,?\d*)\s+Value:\s*\$?(\d+,?\d*)", re.I
)
CLIENT_RE = re.compile(r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)", re.I)
GETDATE_RE = re.compile(r"Date:\s*(\d{4}-\d{2}-\d{2})")


class Constants(Enum):
    UNNAMED_COMPANY = "unnamed"
    SPECTATOR_ID = 255


class GamePhase(Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    GOAL_REACHED = "goal_reached"
    RESETTING = "resetting"
    
    def can_transition_to(self, new_phase):
        transitions = {
            GamePhase.WAITING: [GamePhase.ACTIVE, GamePhase.RESETTING],
            GamePhase.ACTIVE: [GamePhase.GOAL_REACHED, GamePhase.RESETTING],
            GamePhase.GOAL_REACHED: [GamePhase.RESETTING],
            GamePhase.RESETTING: [GamePhase.WAITING, GamePhase.ACTIVE],
        }
        return new_phase in transitions.get(self, [])


class CacheEntry:
    def __init__(self, value, ttl):
        self.value = value
        self.timestamp = time.time()
        self.ttl = ttl
    
    def is_expired(self):
        return time.time() - self.timestamp > self.ttl


class LRUCache:
    def __init__(self, max_size, ttl):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl
        self.lock = threading.RLock()
    
    def get(self, key):
        with self.lock:
            entry = self.cache.get(key)
            if entry:
                if entry.is_expired():
                    del self.cache[key]
                    return None
                self.cache.move_to_end(key)
                return entry.value
            return None
    
    def set(self, key, value):
        with self.lock:
            if key in self.cache:
                del self.cache[key]
            entry = CacheEntry(value, self.ttl)
            self.cache[key] = entry
            self.cache.move_to_end(key)
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)
    
    def delete(self, key):
        with self.lock:
            self.cache.pop(key, None)
    
    def clear(self):
        with self.lock:
            self.cache.clear()
    
    def items(self):
        with self.lock:
            result = []
            expired_keys = []
            for k, entry in self.cache.items():
                if entry.is_expired():
                    expired_keys.append(k)
                else:
                    result.append((k, entry.value))
            for k in expired_keys:
                del self.cache[k]
            return result
    
    def cleanup_expired(self):
        with self.lock:
            expired_keys = [k for k, entry in self.cache.items() if entry.is_expired()]
            for k in expired_keys:
                del self.cache[k]
            return len(expired_keys)


class CircuitBreaker:
    def __init__(self, threshold, timeout):
        self.threshold = threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = 0
        self.is_open = False
        self.lock = threading.Lock()
    
    def call(self, func, *args, **kwargs):
        with self.lock:
            if self.is_open:
                if time.time() - self.last_failure_time > self.timeout:
                    self.is_open = False
                    self.failures = 0
                else:
                    raise Exception("Circuit breaker is open")
        
        try:
            result = func(*args, **kwargs)
            with self.lock:
                self.failures = 0
            return result
        except Exception as e:
            with self.lock:
                self.failures += 1
                self.last_failure_time = time.time()
                if self.failures >= self.threshold:
                    self.is_open = True
            raise e


class RconHandler:
    COMMAND_TIMEOUTS = {
        'companies': 3.0,
        'clients': 3.0,
        'get_date': 2.0,
        'reset_company': 5.0,
        'pause': 2.0,
        'unpause': 2.0,
        'load_scenario': 10.0,
        'move': 2.0,
    }
    
    def __init__(self, admin, logger, config):
        self.admin = admin
        self.logger = logger
        self.config = config
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.buffer = []
        self.inflight = False
        self.end_ts = None
        self.current_cmd = None
        self.circuit_breaker = CircuitBreaker(CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_TIMEOUT)
        self.main_thread = threading.current_thread()
    
    def _escape_rcon(self, text):
        if not text:
            return text
        text = str(text).replace('\\', '\\\\')
        text = text.replace('"', '\\"')
        text = text.replace('\n', ' ')
        text = text.replace('\r', ' ')
        text = text.replace('\t', ' ')
        return text
    
    def _validate_response(self, response, cmd):
        if not response:
            return True
        
        error_indicators = [
            'error', 'failed', 'invalid', 'not found', 'unknown command',
            'usage:', 'syntax error'
        ]
        
        lower_response = response.lower()
        for indicator in error_indicators:
            if indicator in lower_response:
                self.logger.warning("RCON error response for '%s': %s", cmd, response[:200])
                return False
        
        return True
    
    def execute(self, cmd, timeout=None, escape_args=True):
        if escape_args:
            parts = cmd.split(None, 1)
            if len(parts) == 2:
                cmd = f"{parts[0]} {self._escape_rcon(parts[1])}"
        
        cmd_name = cmd.split()[0]
        timeout = timeout or self.COMMAND_TIMEOUTS.get(cmd_name, RCON_TIMEOUT)
        
        for attempt in range(self.config.rcon_retry_max):
            try:
                result = self.circuit_breaker.call(self._execute_once, cmd, timeout)
                if not self._validate_response(result, cmd):
                    if attempt == self.config.rcon_retry_max - 1:
                        return ""
                    time.sleep(self.config.rcon_retry_delay * (attempt + 1))
                    continue
                return result
            except Exception as e:
                if attempt == self.config.rcon_retry_max - 1:
                    self.logger.warning("RCON failed after %d attempts: %s - %s", self.config.rcon_retry_max, cmd, e)
                    return ""
                time.sleep(self.config.rcon_retry_delay * (attempt + 1))
        return ""
    
    def _execute_once(self, cmd, timeout):
        deadline = time.time() + timeout
        
        with self.lock:
            while self.inflight:
                if deadline - time.time() <= 1.0:
                    self.logger.warning("RCON wait nearing timeout; current_cmd=%s blocking new cmd=%s", self.current_cmd, cmd)
                if time.time() >= deadline:
                    self.logger.warning("RCON wait timeout; force-clearing inflight for cmd=%s (stuck on %s)", cmd, self.current_cmd)
                    self.inflight = False
                    self.current_cmd = None
                    self.buffer.clear()
                    self.end_ts = None
                    self.cv.notify_all()
                    raise Exception(f"RCON wait timeout: {cmd}")
                self.cv.wait(0.2)
            
            if len(self.buffer) > RCON_BUFFER_MAX:
                self.buffer = self.buffer[-RCON_BUFFER_MAX//2:]
                self.logger.warning("RCON buffer overflow, trimmed to %d", len(self.buffer))
            
            self.buffer.clear()
            self.inflight = True
            self.end_ts = None
            self.current_cmd = cmd
            self.admin.send_rcon(cmd)
        
        if threading.current_thread() is self.main_thread:
            return self._recv_main(deadline, cmd)
        return self._recv_thread(deadline, cmd)
    
    def _recv_main(self, deadline, cmd):
        while time.time() < deadline:
            for p in self.admin.recv():
                self.admin.handle_packet(p)
            
            with self.lock:
                if not self.inflight and self.end_ts:
                    if time.time() - self.end_ts >= RCON_GRACE:
                        self.current_cmd = None
                        return '\n'.join(self.buffer)
            time.sleep(0.01)
        
        with self.lock:
            self.inflight = False
            self.current_cmd = None
            self.cv.notify_all()
        raise Exception(f"RCON timeout: {cmd}")
    
    def _recv_thread(self, deadline, cmd):
        with self.lock:
            while time.time() < deadline:
                if not self.inflight and self.end_ts:
                    if time.time() - self.end_ts >= RCON_GRACE:
                        self.current_cmd = None
                        return '\n'.join(self.buffer)
                self.cv.wait(0.2)
            self.inflight = False
            self.current_cmd = None
        raise Exception(f"RCON timeout: {cmd}")
    
    def on_rcon(self, text):
        if text:
            with self.lock:
                if self.inflight:
                    if len(self.buffer) < RCON_BUFFER_MAX:
                        self.buffer.append(text)
                    self.cv.notify_all()
    
    def on_rcon_end(self):
        with self.lock:
            if self.inflight:
                self.end_ts = time.time()
                self.inflight = False
                self.logger.debug("RCON end received for cmd=%s", self.current_cmd)
                self.cv.notify_all()
    
    def batch_execute(self, commands):
        results = {}
        for cmd in commands:
            results[cmd] = self.execute(cmd)
        return results


class SafeAdmin:
    def __init__(self, *args, **kwargs):
        self._admin = Admin(*args, **kwargs)
        self._send_lock = threading.Lock()
        self.connected = False
        self.reconnect_lock = threading.Lock()
    
    def _send(self, packet):
        with self._send_lock:
            self._admin._send(packet)
    
    def __getattr__(self, name):
        return getattr(self._admin, name)
    
    def connect(self, admin_name, admin_pass, logger):
        try:
            self._admin.login(admin_name, admin_pass)
            self.connected = True
            logger.info("Connected successfully")
            return True
        except Exception as e:
            logger.error("Connection failed: %s", e)
            self.connected = False
            return False
    
    def ensure_connected(self, admin_name, admin_pass, logger):
        with self.reconnect_lock:
            if not self.connected:
                logger.info("Attempting reconnect...")
                return self.connect(admin_name, admin_pass, logger)
            return True


class CommandHandler:
    def __init__(self, bot):
        self.bot = bot
    
    def execute(self, cid, args, state):
        raise NotImplementedError


class HelpCommand(CommandHandler):
    def execute(self, cid, args, state):
        self.bot.send_msg("Commands: !info, !rules, !cv, !reset", cid)


class InfoCommand(CommandHandler):
    def execute(self, cid, args, state):
        info = [
            "=== Server Info ===",
            "South-East-Asia OpenTTD Server",
            "Gamescript: Production Booster on primary industries",
            "Transport >70% boosts production, <50% reduces",
            f"Goal: First company value ${self.bot._fmt(self.bot.cfg.goal_value)} wins"
        ]
        self.bot.send_msg('\n'.join(info), cid)


class RulesCommand(CommandHandler):
    def execute(self, cid, args, state):
        rules = [
            "=== Rules ===",
            "1. No griefing/sabotage",
            "2. No blocking players",
            "3. No cheating/exploits",
            "4. Be respectful",
            f"5. Inactive >{self.bot.cfg.dead_co_age}y & <${self.bot._fmt(self.bot.cfg.dead_co_value)} auto-reset",
            "6. Admin decisions final"
        ]
        self.bot.send_msg('\n'.join(rules), cid)


class CompanyValueCommand(CommandHandler):
    def execute(self, cid, args, state):
        state = self.bot._refresh_game_state()
        companies = state.get('companies', {})
        clients = state.get('clients', {})
        
        active_companies = self.bot._get_active_companies(companies, clients)
        
        if not active_companies:
            self.bot.send_msg("No companies", cid)
            return
        
        sorted_cos = sorted(active_companies.items(), key=lambda x: x[1]['value'], reverse=True)
        lines = ["=== Company Value Rankings ==="]
        
        for i, (co_id, data) in enumerate(sorted_cos[:10], 1):
            pct = (data['value'] / self.bot.cfg.goal_value) * 100
            lines.append(f"{i}. {data['name']} (#{data['display_id']}): ${self.bot._fmt(data['value'])} ({pct:.1f}%)")
        
        self.bot.send_msg('\n'.join(lines), cid)


class ResetCommand(CommandHandler):
    def execute(self, cid, args, state):
        with self.bot.reset_lock:
            if cid in self.bot.reset_pending:
                self.bot.send_msg("Reset already pending. Type !yes to confirm or wait for timeout.", cid)
                return
        
        co_id = self.bot._get_client_company(cid)
        if co_id is None or co_id == Constants.SPECTATOR_ID.value:
            self.bot.send_msg("Must be in company to reset", cid)
            return
        
        with self.bot.reset_lock:
            if cid in self.bot.reset_timers:
                self.bot.reset_timers[cid].cancel()
                self.bot.reset_timers[cid] = None
            
            self.bot.reset_pending[cid] = co_id
        
        msg = f"=== Reset Company {co_id} ===\nThis DELETES your company!\nType !yes to confirm (30s timeout)"
        self.bot.send_msg(msg, cid)
        
        timer = threading.Timer(RESET_TIMEOUT, self.bot._cancel_reset, args=[cid])
        with self.bot.reset_lock:
            self.bot.reset_timers[cid] = timer
            timer.start()
        self.bot.logger.info("Reset req: c=%d co=%d", cid, co_id)


class YesCommand(CommandHandler):
    def execute(self, cid, args, state):
        with self.bot.reset_lock:
            if cid not in self.bot.reset_pending:
                self.bot.send_msg("No pending reset. Use !reset first", cid)
                return
            
            pending_co_id = self.bot.reset_pending.pop(cid)
            if cid in self.bot.reset_timers:
                timer = self.bot.reset_timers.pop(cid, None)
                if timer:
                    timer.cancel()
        
        current_co = self.bot._get_client_company(cid)
        if current_co != pending_co_id:
            self.bot.send_msg(
                f"Reset cancelled: you were in company {pending_co_id} but are now in company {current_co if current_co != Constants.SPECTATOR_ID.value else 'spectator'}. "
                "Run !reset again from the company you want to delete.",
                cid
            )
            return
        
        self.bot._update_companies_from_rcon(reason="reset_verify")
        
        if not self.bot.companies_cache.get(pending_co_id):
            self.bot.send_msg(f"Company {pending_co_id} no longer exists.", cid)
            return
        
        current_co_verify = self.bot._get_client_company(cid)
        if current_co_verify != pending_co_id:
            self.bot.send_msg(
                f"Reset cancelled: company mismatch detected (expected {pending_co_id}, got {current_co_verify})",
                cid
            )
            return
        
        self.bot.logger.info("Reset confirm: c=%d co=%d", cid, pending_co_id)
        self.bot.rcon.execute(f'move {cid} {Constants.SPECTATOR_ID.value}', escape_args=False)
        time.sleep(0.2)
        
        self.bot.rcon.execute(f"reset_company {pending_co_id}", escape_args=False)
        self.bot._update_companies_from_rcon(reason="reset_confirm")
        self.bot.companies_cache.delete(pending_co_id)
        
        self.bot.send_msg(f"Company {pending_co_id} reset", cid)
        self.bot.logger.info("Reset done: co=%d", pending_co_id)
        self.bot._schedule_pause_check()


class OpenTTDBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.logger = logging.getLogger(f"[S{self.cfg.server_num}]")
        self._validate_config()
        
        self.admin = SafeAdmin(ip=self.cfg.server_ip, port=self.cfg.admin_port)
        self.stop_event = threading.Event()
        self.rcon = None
        
        self.companies_cache = LRUCache(CACHE_MAX_SIZE, CACHE_TTL)
        self.clients_cache = LRUCache(CACHE_MAX_SIZE, CACHE_TTL)
        self.cache_ts = 0.0
        self.state_initialized = False
        self.last_companies_rcon = 0.0
        self.last_clients_rcon = 0.0
        
        self.phase = GamePhase.WAITING
        self.phase_lock = threading.Lock()
        self.paused = None
        
        self.pause_timer = None
        self.pause_timer_lock = threading.Lock()
        self.pause_check_scheduled = False
        self.pause_check_lock = threading.Lock()
        
        self.reset_lock = threading.Lock()
        self.reset_pending = {}
        self.reset_timers = {}

        self.paused_lock = threading.Lock()
        
        self.cleanup_lock = threading.Lock()
        self.cleanup_in_progress = set()
        self.cleanup_semaphore = threading.Semaphore(CLEANUP_SEMAPHORE_MAX)
        
        self.command_cooldown = {}
        self.command_cooldown_lock = threading.Lock()
        
        worker_count = min(max(2, len(SERVERS)), 8)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix=f"Bot{cfg.server_num}"
        )
        
        self.commands = {
            'help': HelpCommand(self),
            'info': InfoCommand(self),
            'rules': RulesCommand(self),
            'cv': CompanyValueCommand(self),
            'reset': ResetCommand(self),
            'yes': YesCommand(self),
        }
        
        self.memory_tracker = None
        self.last_cleanup = time.time()
        self.reconnect_attempts = 0
        
        self.logger.info("Bot init: server=%s:%d (workers=%d)", self.cfg.server_ip, self.cfg.admin_port, worker_count)
    
    def _validate_config(self):
        if not (1024 <= self.cfg.admin_port <= 65535):
            raise ValueError(f"Invalid admin_port: {self.cfg.admin_port}")
        if self.cfg.game_port:
            if not (1024 <= self.cfg.game_port <= 65535):
                raise ValueError(f"Invalid game_port: {self.cfg.game_port}")
        if self.cfg.goal_value <= 0:
            raise ValueError(f"Invalid goal_value: {self.cfg.goal_value}")
        if not self.cfg.load_scenario:
            raise ValueError("load_scenario cannot be empty")
    
    def start(self):
        self.admin.add_handler(openttdpacket.ChatPacket)(self.on_chat)
        self.admin.add_handler(openttdpacket.ClientJoinPacket)(self.on_join)
        self.admin.add_handler(openttdpacket.ClientInfoPacket)(self.on_client_info)
        self.admin.add_handler(openttdpacket.ClientUpdatePacket)(self.on_update)
        self.admin.add_handler(openttdpacket.ClientQuitPacket)(self.on_client_quit)
        self.admin.add_handler(openttdpacket.RconPacket)(self.on_rcon)
        self.admin.add_handler(openttdpacket.RconEndPacket)(self.on_rcon_end)
        self.admin.add_handler(openttdpacket.NewGamePacket)(self.on_new_game)
        
        try:
            while self.reconnect_attempts < self.cfg.reconnect_max_attempts and not self.stop_event.is_set():
                try:
                    self.logger.info("Connecting: %s:%d (attempt %d/%d)", 
                                   self.cfg.server_ip, self.cfg.admin_port,
                                   self.reconnect_attempts + 1, self.cfg.reconnect_max_attempts)
                    
                    if not self.admin.connect(self.cfg.admin_name, self.cfg.admin_pass, self.logger):
                        raise Exception("Initial connection failed")
                    
                    self.reconnect_attempts = 0
                    self.rcon = RconHandler(self.admin, self.logger, self.cfg)
                    
                    self.admin.subscribe(AdminUpdateType.CHAT)
                    self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
                    
                    time.sleep(0.5)
                    self._batch_refresh_state(reason="startup")
                    self.state_initialized = True
                    
                    self._cleanup_unnamed_company()
                    self._monitor_tick()
                    
                    for target, name in [
                        (self._game_monitor_loop, "GameMonitor"),
                        (self._company_refresh_loop, "CompanyRefresh"),
                        (self._periodic_cleanup_loop, "PeriodicCleanup"),
                        (self._memory_monitor_loop, "MemoryMonitor"),
                        (self._cache_cleanup_loop, "CacheCleanup"),
                    ]:
                        try:
                            threading.Thread(target=target, daemon=True, name=name).start()
                        except Exception as e:
                            self.logger.error("Failed to start thread %s: %s", name, e)
                    
                    self.send_msg("Admin connected")
                    self.logger.info("Main loop start")
                    
                    while not self.stop_event.is_set():
                        try:
                            if not self.admin.ensure_connected(self.cfg.admin_name, self.cfg.admin_pass, self.logger):
                                raise ConnectionError("Connection lost")
                            
                            for p in self.admin.recv():
                                self.admin.handle_packet(p)
                        except ConnectionError as e:
                            self.logger.error("Connection lost: %s", e)
                            self.admin.connected = False
                            raise
                        except Exception as e:
                            self.logger.warning("Recv error: %s", e)
                        self.stop_event.wait(0.1)
                    
                    break
                
                except (ConnectionError, Exception) as e:
                    self.logger.error("Connection error: %s", e)
                    self.admin.connected = False
                    self.reconnect_attempts += 1
                    
                    if self.reconnect_attempts < self.cfg.reconnect_max_attempts:
                        delay = self.cfg.reconnect_delay * self.reconnect_attempts
                        self.logger.info("Reconnecting in %.1fs...", delay)
                        self.stop_event.wait(delay)
                    else:
                        self.logger.error("Max reconnection attempts reached")
                        break
        
        except KeyboardInterrupt:
            self.logger.info("Shutdown: user interrupt")
        except Exception as e:
            self.logger.error("Fatal: %s", e, exc_info=True)
        finally:
            self._cleanup()
    
    def _cleanup(self):
        self.logger.info("Cleanup start")
        self.stop_event.set()
        
        with self.pause_timer_lock:
            if self.pause_timer:
                self.pause_timer.cancel()
                self.pause_timer = None
        
        with self.reset_lock:
            for cid, timer in list(self.reset_timers.items()):
                if timer:
                    timer.cancel()
            self.reset_timers.clear()
            self.reset_pending.clear()
        
        try:
            self.executor.shutdown(wait=True, timeout=EXECUTOR_SHUTDOWN_TIMEOUT)
        except TypeError:
            self.executor.shutdown(wait=False)
        except Exception as e:
            self.logger.warning("Executor shutdown error: %s", e)
        
        if self.memory_tracker:
            tracemalloc.stop()
        
        self.logger.info("Cleanup done")
    
    def _periodic_cleanup_loop(self):
        while not self.stop_event.wait(CLEANUP_INTERVAL):
            try:
                now = time.time()
                with self.reset_lock:
                    expired = []
                    for cid, timer in list(self.reset_timers.items()):
                        if timer is None or not timer.is_alive():
                            expired.append(cid)
                    for cid in expired:
                        self.reset_timers.pop(cid, None)
                        self.reset_pending.pop(cid, None)
                
                with self.cleanup_lock:
                    self.cleanup_in_progress.clear()
                
                with self.command_cooldown_lock:
                    old_cooldowns = [cid for cid, ts in self.command_cooldown.items() 
                                    if now - ts > 60]
                    for cid in old_cooldowns:
                        self.command_cooldown.pop(cid, None)
                
                self.last_cleanup = now
                self.logger.debug("Periodic cleanup completed")
            except Exception as e:
                self.logger.error("Cleanup error: %s", e)
    
    def _cache_cleanup_loop(self):
        while not self.stop_event.wait(60):
            try:
                expired_companies = self.companies_cache.cleanup_expired()
                expired_clients = self.clients_cache.cleanup_expired()
                if expired_companies or expired_clients:
                    self.logger.debug("Cache cleanup: %d companies, %d clients expired", 
                                    expired_companies, expired_clients)
            except Exception as e:
                self.logger.error("Cache cleanup error: %s", e)
    
    def _memory_monitor_loop(self):
        try:
            tracemalloc.start()
            self.memory_tracker = True
        except Exception:
            return
        
        while not self.stop_event.wait(MEMORY_CHECK_INTERVAL):
            try:
                current, peak = tracemalloc.get_traced_memory()
                self.logger.info("Memory: current=%.1fMB peak=%.1fMB", 
                               current / 1024 / 1024, peak / 1024 / 1024)
            except Exception as e:
                self.logger.debug("Memory check error: %s", e)
    
    def _check_command_cooldown(self, cid):
        with self.command_cooldown_lock:
            now = time.time()
            last_cmd = self.command_cooldown.get(cid, 0)
            if now - last_cmd < COMMAND_COOLDOWN:
                return False
            self.command_cooldown[cid] = now
            return True
    
    def on_chat(self, admin, pkt):
        msg = pkt.message.strip()
        cid = pkt.id
        
        if msg.startswith('!'):
            if not self._check_command_cooldown(cid):
                return
            self.logger.info("CMD: c=%s msg=%s", cid, msg)
            self.executor.submit(self._process_cmd, cid, msg)
    
    def on_join(self, admin, pkt):
        cid = pkt.id
        self.logger.info("Join: c=%s", cid)
        self.executor.submit(self._cleanup_unnamed_company)
        self.executor.submit(self._greet, cid)
    
    def on_client_info(self, admin, pkt):
        cid = pkt.id
        self.clients_cache.set(cid, {
            'name': pkt.name,
            'company': pkt.company_id,
        })
        self.cache_ts = time.time()
        self._schedule_pause_check()
    
    def on_client_quit(self, admin, pkt):
        cid = pkt.id
        with self.reset_lock:
            if cid in self.reset_pending:
                self.reset_pending.pop(cid, None)
                timer = self.reset_timers.pop(cid, None)
                if timer:
                    timer.cancel()
        self.clients_cache.delete(cid)
        self.cache_ts = time.time()
    
    def on_update(self, admin, pkt):
        cid = pkt.id
        co_id = pkt.company_id
        
        cached = self.clients_cache.get(cid) or {}
        self.clients_cache.set(cid, {
            'name': cached.get('name', pkt.name),
            'company': co_id
        })
        self.cache_ts = time.time()
        
        if cached.get('company') != co_id:
            with self.reset_lock:
                if cid in self.reset_pending:
                    self.reset_pending.pop(cid, None)
                    timer = self.reset_timers.pop(cid, None)
                    if timer:
                        timer.cancel()
                    try:
                        self.send_msg("Reset cancelled: you switched companies", cid)
                    except Exception:
                        pass
        
        self._schedule_pause_check()
    
    def on_new_game(self, admin, pkt):
        self.logger.info("New game/map detected")
        time.sleep(0.5)
        self.companies_cache.clear()
        self.clients_cache.clear()
        self.cache_ts = 0.0
        self.state_initialized = False
        self._cleanup_unnamed_company()
        self._schedule_pause_check()
    
    def on_rcon(self, admin, pkt):
        if self.rcon:
            self.rcon.on_rcon(pkt.response.strip())
    
    def on_rcon_end(self, admin, pkt):
        if self.rcon:
            self.rcon.on_rcon_end()
    
    def _schedule_pause_check(self):
        with self.pause_check_lock:
            if not self.pause_check_scheduled:
                self.pause_check_scheduled = True
                threading.Timer(PAUSE_DEBOUNCE, self._run_pause_check).start()
    
    def _run_pause_check(self):
        with self.pause_check_lock:
            self.pause_check_scheduled = False
        self.executor.submit(self._check_pause_state)
    
    def _cache_valid(self):
        return time.time() - self.cache_ts < CACHE_TTL
    
    def _refresh_game_state(self):
        need_refresh = not self._cache_valid()
        now = time.time()
        
        if (now - self.last_companies_rcon >= RCON_REFRESH_TTL) or \
           (now - self.last_clients_rcon >= RCON_REFRESH_TTL):
            need_refresh = True
        
        if need_refresh:
            self._batch_refresh_state(reason="cache_refresh")
        
        companies = {k: v for k, v in self.companies_cache.items()}
        clients = {k: v for k, v in self.clients_cache.items()}
        
        if not companies:
            has_client_company = any(
                c.get('company') not in (None, Constants.SPECTATOR_ID.value) 
                for c in clients.values()
            )
            if has_client_company:
                self._batch_refresh_state(reason="clients_hint")
                companies = {k: v for k, v in self.companies_cache.items()}
        
        return {'companies': companies, 'clients': clients}
    
    def _batch_refresh_state(self, reason=""):
        now = time.time()
        commands = []
        
        if now - self.last_companies_rcon >= RCON_REFRESH_TTL:
            commands.append('companies')
        if now - self.last_clients_rcon >= RCON_REFRESH_TTL:
            commands.append('clients')
        
        if not commands:
            return
        
        results = self.rcon.batch_execute(commands)
        
        if 'companies' in results:
            self._parse_companies(results['companies'], reason)
        if 'clients' in results:
            self._parse_clients(results['clients'])
    
    def _parse_companies(self, output, reason=""):
        if not output.strip():
            return
        
        updated = 0
        for line in output.splitlines():
            m = COMPANY_RE.match(line)
            if not m:
                continue
            try:
                parsed_id, parsed_name, year, money, loan, value = m.groups()
                co_id = int(parsed_id)
                self.companies_cache.set(co_id, {
                    'display_id': co_id,
                    'name': parsed_name.strip(),
                    'start_date': int(year),
                    'money': int(money.replace(',', '')),
                    'loan': int(loan.replace(',', '')),
                    'value': int(value.replace(',', '')),
                })
                updated += 1
            except (ValueError, AttributeError) as e:
                self.logger.debug("Company parse failed: %s (line=%s)", e, line)
                continue
        
        if updated:
            self.cache_ts = time.time()
            self.state_initialized = True
            self.last_companies_rcon = time.time()
            if reason:
                self.logger.info("Companies refreshed: %s (updated=%d)", reason, updated)
    
    def _parse_clients(self, output):
        if not output.strip():
            return

        for line in output.splitlines():
            m = CLIENT_RE.match(line)
            if not m:
                continue
            try:
                parsed_cid, parsed_name, parsed_co = m.groups()
                cid = int(parsed_cid)
                parsed_co_int = int(parsed_co)
                company_id = parsed_co_int if parsed_co_int != Constants.SPECTATOR_ID.value else Constants.SPECTATOR_ID.value

                existing = self.clients_cache.get(cid) or {}
                self.clients_cache.set(cid, {
                    'name': existing.get('name', parsed_name),
                    'company': company_id
                })
            except (ValueError, AttributeError) as e:
                self.logger.debug("Client parse failed: %s (line=%s)", e, line)
                continue
        
        self.last_clients_rcon = time.time()
    
    def _get_current_year(self, companies):
        out = self.rcon.execute('get_date')
        if out:
            m = GETDATE_RE.search(out)
            if m:
                year_str = m.group(1)
                try:
                    parsed = datetime.datetime.strptime(year_str, "%Y-%m-%d").date()
                    return int(parsed.year)
                except ValueError:
                    return int(year_str.split('-')[0])
        if companies:
            max_year = max((co.get("start_date", 0) for co in companies.values()), default=GAME_START_YEAR)
            return int(max_year)
        return GAME_START_YEAR
    
    def _update_companies_from_rcon(self, reason=""):
        output = self.rcon.execute('companies')
        self._parse_companies(output, reason)

    def _update_clients_from_rcon(self, reason=""):
        output = self.rcon.execute('clients')
        if output:
            self._parse_clients(output)
            if reason:
                self.logger.info("Clients refreshed: %s", reason)
    
    def _company_refresh_loop(self):
        while not self.stop_event.wait(60):
            with self.paused_lock:
                is_paused = self.paused is True
            if is_paused:
                continue
            with self.phase_lock:
                phase = self.phase
            if phase != GamePhase.ACTIVE:
                continue
            self._update_companies_from_rcon(reason="periodic")
    
    def _cleanup_unnamed_company(self):
        try:
            self._update_companies_from_rcon(reason="unnamed_check")
            for co_id, co_data in list(self.companies_cache.items()):
                try:
                    co_name = (co_data.get('name') or '').strip().lower()
                    if co_name == Constants.UNNAMED_COMPANY.value:
                        with self.cleanup_lock:
                            if co_id in self.cleanup_in_progress:
                                return False
                            self.cleanup_in_progress.add(co_id)

                        try:
                            self.logger.info("Deleting unnamed company: id=%s", co_id)
                            self.rcon.execute(f"reset_company {co_id}", escape_args=False)
                            time.sleep(0.1)
                            self.companies_cache.delete(co_id)
                            return True
                        finally:
                            with self.cleanup_lock:
                                self.cleanup_in_progress.discard(co_id)
                except Exception as e:
                    self.logger.debug("Unnamed cleanup parse failed for co=%s: %s", co_id, e)
                    continue
            return False
        except Exception:
            self.logger.exception("Unnamed company cleanup failed")
            return False
    
    def _get_active_companies(self, companies, clients):
        active = {}
        for co_id, co_data in companies.items():
            co_name = (co_data.get('name') or '').strip()
            has_players = any(c.get('company') == co_id for c in clients.values())
            if co_name.lower() == Constants.UNNAMED_COMPANY.value and not has_players:
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
        with self.paused_lock:
            should_pause = len(active_companies) == 0
        
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
                with self.paused_lock:
                    currently_paused = self.paused
                if currently_paused is not False:
                    needs_unpause = True
        
        if needs_unpause:
            self.rcon.execute('unpause')
            with self.paused_lock:
                self.paused = False
            self.logger.info("Game unpaused: %d active companies", len(active_companies))
    
    def _do_pause(self):
        with self.pause_timer_lock:
            self.pause_timer = None
        
        with self.paused_lock:
            already_paused = self.paused
        if not already_paused:
            self.rcon.execute('pause')
            with self.paused_lock:
                self.paused = True
            self.logger.info("Game paused: no active companies")
    
    def _transition_phase(self, new_phase):
        if self.phase == new_phase:
            return True
        
        if self.phase.can_transition_to(new_phase):
            old_phase = self.phase
            self.phase = new_phase
            self.logger.info("Phase transition: %s -> %s", old_phase.value, new_phase.value)
            return True
        else:
            self.logger.warning("Invalid phase transition: %s -> %s", self.phase.value, new_phase.value)
            return False
    
    def _load_scenario(self):
        self.logger.info("Loading: %s", self.cfg.load_scenario)
        resp = self.rcon.execute(f"load_scenario {self.cfg.load_scenario}", escape_args=False)
        
        if resp and "cannot be found" in resp.lower():
            self.logger.error("Scenario not found: %s", self.cfg.load_scenario)
            return False
        
        self.companies_cache.clear()
        self.cache_ts = 0.0
        
        self.logger.info("Scenario loaded: %s", self.cfg.load_scenario)
        time.sleep(0.5)
        self._cleanup_unnamed_company()
        return True
    
    def send_msg(self, msg, cid=None):
        for line in msg.split('\n'):
            try:
                if cid:
                    self.admin._chat(line, action=openttdpacket.Actions.SERVER_MESSAGE,
                                   desttype=openttdpacket.ChatDestTypes.CLIENT, id=cid)
                else:
                    self.admin._chat(line, action=openttdpacket.Actions.SERVER_MESSAGE,
                                   desttype=openttdpacket.ChatDestTypes.BROADCAST)
            except Exception as e:
                self.logger.error("Send failed: %s", e)
            time.sleep(MSG_RATE_LIMIT)
    
    def _game_monitor_loop(self):
        self.logger.info("Monitor loop start")
        while not self.stop_event.is_set():
            interval = self._monitor_tick()
            self.stop_event.wait(interval)
        self.logger.info("Monitor loop stop")
    
    def _monitor_tick(self):
        try:
            self._refresh_game_state()
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
            self.logger.error("Monitor error: %s", e)
            return MONITOR_INTERVAL_DEFAULT
    
    def _check_goal(self, companies):
        if not companies:
            return MONITOR_INTERVAL_NO_CO
        
        top_id, top = max(companies.items(), key=lambda x: x[1]['value'])
        ratio = top['value'] / self.cfg.goal_value
        
        if top['value'] >= self.cfg.goal_value:
            with self.phase_lock:
                if self.phase in (GamePhase.GOAL_REACHED, GamePhase.RESETTING):
                    return MONITOR_INTERVAL_DEFAULT

                self.logger.info("GOAL! %s=$%s", top['name'], self._fmt(top['value']))
                transitioned = self._transition_phase(GamePhase.GOAL_REACHED)
            if transitioned:
                self._handle_goal(top['name'], top['value'])
            return MONITOR_INTERVAL_DEFAULT
        
        if ratio >= 0.95:
            return MONITOR_INTERVAL_95PCT
        elif ratio >= 0.9:
            return MONITOR_INTERVAL_90PCT
        else:
            return MONITOR_INTERVAL_DEFAULT
    
    def _check_dead_companies(self, companies, clients):
        if not companies:
            return
        
        year = self._get_current_year(companies)
        dead_companies = []
        
        for co_id, co_data in companies.items():
            founded = co_data.get('start_date')
            if not founded or founded <= GAME_START_YEAR:
                continue
            
            age = year - founded
            value = co_data.get('value', 0)
            
            if age >= self.cfg.dead_co_age and value < self.cfg.dead_co_value:
                dead_companies.append((co_id, co_data))
        
        futures = []
        for co_id, co_data in dead_companies:
            future = self.executor.submit(self._cleanup_company, co_id, co_data)
            futures.append(future)
        
        for future in futures:
            try:
                future.result(timeout=10)
            except Exception as e:
                self.logger.error("Cleanup future failed: %s", e)
    
    def _cleanup_company(self, co_id, co_data):
        with self.cleanup_lock:
            if co_id in self.cleanup_in_progress:
                self.logger.debug("Cleanup already in progress for co=%d", co_id)
                return
            self.cleanup_in_progress.add(co_id)
        
        try:
            if not self.cleanup_semaphore.acquire(blocking=False):
                self.logger.warning("Cleanup semaphore full, skipping co=%d", co_id)
                with self.cleanup_lock:
                    self.cleanup_in_progress.discard(co_id)
                return
            
            try:
                self._update_clients_from_rcon(reason="cleanup_company")
                name = co_data.get('name', f"Co {co_id}")
                co_clients = [c for c, d in self.clients_cache.items() if d.get('company') == co_id]
                
                self.logger.info("Cleanup: co=%d n=%s cl=%d", co_id, name, len(co_clients))
                
                for cid in co_clients:
                    self.rcon.execute(f'move {cid} {Constants.SPECTATOR_ID.value}', escape_args=False)
                    time.sleep(0.02)
                
                self.rcon.execute(f"reset_company {co_id}", escape_args=False)
                self.companies_cache.delete(co_id)
                self.send_msg(f"Dead company cleanup: {name}")
                self.logger.info("Cleanup done: co=%d", co_id)
            finally:
                self.cleanup_semaphore.release()
        finally:
            with self.cleanup_lock:
                self.cleanup_in_progress.discard(co_id)
    
    def _handle_goal(self, winner, value):
        try:
            with self.phase_lock:
                if self.phase != GamePhase.GOAL_REACHED:
                    return
                self._transition_phase(GamePhase.RESETTING)

            msg = (
                "=== GOAL ACHIEVED ===\n"
                f"Winner: {winner}\n"
                f"Goal: ${self._fmt(self.cfg.goal_value)}\n"
                f"Map restart in {RESET_COUNTDOWN_SECONDS}s..."
            )
            self.send_msg(msg)
            threading.Thread(target=self._reset_countdown, daemon=True).start()
        except Exception:
            self.logger.exception("Goal handler failed")
    
    def _reset_countdown(self):
        try:
            self.logger.info("Reset countdown: %ds", RESET_COUNTDOWN_SECONDS)
            for i in range(RESET_COUNTDOWN_SECONDS, 0, -1):
                if i in (10, 5):
                    self.send_msg(f"Map reset in {i}s...")
                if self.stop_event.wait(1):
                    self.logger.info("Reset countdown aborted")
                    return
            
            if self._load_scenario():
                self.send_msg("New map loaded")
                with self.phase_lock:
                    self._transition_phase(GamePhase.WAITING)
                with self.paused_lock:
                    self.paused = False
                self._schedule_pause_check()
            else:
                self.send_msg("Map reset failed!")
                with self.phase_lock:
                    self._transition_phase(GamePhase.ACTIVE)
        except Exception:
            self.logger.exception("Reset countdown failed")
    
    def _get_client_name(self, cid):
        cached = self.clients_cache.get(cid)
        if cached and cached.get('name'):
            return cached['name']

        self._update_clients_from_rcon(reason="name_lookup")

        cached = self.clients_cache.get(cid)
        if cached and cached.get('name'):
            return cached['name']

        return f"C{cid}"
    
    def _get_client_company(self, cid):
        cached = self.clients_cache.get(cid)
        if cached and 'company' in cached:
            return cached['company']

        self._update_clients_from_rcon(reason="company_lookup")

        cached = self.clients_cache.get(cid)
        if cached and 'company' in cached:
            return cached['company']

        return None
    
    def _greet(self, cid):
        if self.stop_event.wait(GREETING_DELAY):
            return
        name = self._get_client_name(cid)
        self.logger.info("Greeting c=%s n=%s", cid, name)
        self.send_msg(f"Welcome {name}! Type !help for commands", cid)
    
    def _process_cmd(self, cid, msg):
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
            handler.execute(cid, args, state)
        except Exception as e:
            self.logger.error("CMD error: cmd=%s c=%d e=%s", cmd, cid, e)
            self.send_msg("Command failed", cid)
    
    def _cancel_reset(self, cid):
        with self.reset_lock:
            self.reset_pending.pop(cid, None)
            timer = self.reset_timers.pop(cid, None)
            if timer:
                timer.cancel()
    
    def _fmt(self, val):
        if val >= 1_000_000_000:
            return f"{val/1_000_000_000:.1f}B"
        if val >= 1_000_000:
            return f"{val/1_000_000:.1f}M"
        if val >= 1_000:
            return f"{val/1_000:.1f}k"
        return str(val)


def signal_handler(signum, frame):
    root_logger.info("Received signal %d, shutting down...", signum)
    for bot in _running_bots:
        bot.stop_event.set()
    sys.exit(0)


_running_bots = []

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    root_logger.info('=== OpenTTD Admin Bot Starting ===')
    root_logger.info('Admin Ports: 3967-3976')
    root_logger.info('Game Ports: 3979-3988')
    
    threads = []
    for cfg in SERVERS:
        bot = OpenTTDBot(cfg)
        _running_bots.append(bot)
        t = threading.Thread(target=bot.start, name=f'Bot-{cfg.server_num}')
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
