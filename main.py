from __future__ import annotations

import asyncio
import contextlib
import configparser
import json
import logging
import os
import re
import signal
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from aiopyopenttdadmin import Admin, AdminUpdateType, AdminUpdateFrequency, openttdpacket
from pyopenttdadmin.enums import Actions, ChatDestTypes

_RCON_COMPANY_RE = re.compile(
    r"#:(\d+)\([^)]+\)\s+Company Name:\s+'([^']+)'\s+Year Founded:\s+(\d+).*?Value:\s+(\d+)"
)

_RECONNECT_DELAY        = 30       # seconds between reconnect attempts
_RECV_TIMEOUT           = 1.0      # seconds for idle recv in main loop
_COUNTRY_CACHE_MAX      = 256      # max IP→country cache entries
_CMD_COOLDOWN           = 2.0      # minimum seconds between !commands per client
_RESET_TIMEOUT          = 15       # seconds for voluntary company reset confirmation
_ADMIN_CLIENT_NAME      = "Admin"  # name used by the admin login; skip greeting
_SPECTATOR              = 255      # company ID sentinel for spectators
_RCON_JOINED_SPEC       = "has joined spectators"
_RCON_CO_DELETED        = "Company deleted"
_MAP_NEWGAME            = "newgame"
_COOLDOWN_TTL           = 300      # seconds before idle cooldown entries are evicted

@dataclass
class Company:
    name: str = ""
    founded: int = 0
    value: int = 0

@dataclass
class Client:
    name: str
    company_id: int
    ip: str = "0.0.0.0"

def fmt(value: int) -> str:
    """Format integer with b/m/k suffix."""
    for threshold, suffix in [(1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")]:
        if value >= threshold:
            return f"{value / threshold:.1f}{suffix}"
    return str(value)

def load_config(path: str = "settings.cfg") -> list[dict[str, Any]]:
    """Parse settings.cfg; return list of server config dicts."""
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    def _parse(section):
        entry = {"name": section}
        for key, val in cfg.items(section):
            low = val.lower()
            entry[key] = int(val) if val.isdigit() else (low == "true") if low in ("true", "false") else val
        return entry
    return [_parse(s) for s in cfg.sections()]

def normalize_config(cfg: dict[str, Any], log: logging.Logger) -> list[str]:
    """Validate and normalize config in-place; return fatal error strings."""
    for field in ("ip", "port", "admin_name", "admin_pass"):
        if not cfg.get(field):
            return [f"Missing required field: {field}"]
    if not (isinstance(cfg.get("port"), int) and 1 <= cfg["port"] <= 65535):
        return [f"Invalid port: {cfg.get('port')}"]
    cfg.setdefault("debug", False)
    cfg.setdefault("map", "")
    cfg.setdefault("max_companies", 2)
    cfg.setdefault("broadcast_cv", 3600)
    cfg["goal"] = g if isinstance(g := cfg.get("goal", 0), int) and g > 0 else 0
    if not cfg["goal"]:
        cfg["map"] = ""
    age, val = cfg.get("clean_age", 0), cfg.get("clean_value", 0)
    if not all(isinstance(x, int) and x > 0 for x in (age, val)):
        cfg["clean_age"] = cfg["clean_value"] = 0
        log.info("auto-clean disabled")
    log.info(f"[{cfg['name']}] goal:{cfg['goal']} map:{cfg['map'] or 'disabled'}")
    return []

class Bot:
    """OpenTTD admin bot: enforces company limits, manages pause, goal, and auto-clean."""

    def __init__(self, cfg: dict[str, Any], log: logging.Logger) -> None:
        self.cfg, self.log = cfg, log
        self.admin: Admin | None = None
        self.running = False
        self._lock = asyncio.Lock()
        self.companies: dict[int, Company] = {}
        self.company_owners: dict[int, int] = {}
        self.clients: dict[int, Client] = {}
        self.game_year = 0
        self.game_date = 0
        self.is_paused = True
        self.goal_reached = False
        self.cooldowns: dict[int, float] = {}
        self.reset_pending: dict[int, tuple[int, float]] = {}
        self._cleaning: set[int] = set()
        self._enforcing: set[int] = set()
        self.last_pause_cmd: bool | None = None
        self.last_cmd_time = 0.0
        self._pause_retry_at: float = 0.0
        self.tasks: set[asyncio.Task] = set()
        self._client_ready: dict[int, asyncio.Event] = {}
        self._new_game_event = asyncio.Event()
        self._rcon_lock = asyncio.Lock()
        self._drain_depth = 0
        self._country_cache: dict[str, str] = {}

    @staticmethod
    def _to_cid(raw: int) -> int:
        """0-based → 1-based company ID; spectator unchanged."""
        return _SPECTATOR if raw == _SPECTATOR else raw + 1

    def _spawn(self, coro) -> asyncio.Task:
        """Fire-and-forget task; auto-removes from self.tasks on completion."""
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def _pkt_cid(self, pkt: Any) -> int:
        """Extract 1-based company ID from a client packet."""
        return self._to_cid(getattr(pkt, "play_as", getattr(pkt, "company_id", _SPECTATOR)))

    def _check_ownership(self, client_id: int, co: int) -> bool:
        """Assign ownership if new; return True if client exceeded max_companies limit. Call under _lock."""
        if co == _SPECTATOR or co not in self.companies or co in self._enforcing or co in self.company_owners:
            return False
        if sum(1 for v in self.company_owners.values() if v == client_id) >= self.cfg["max_companies"]:
            return True
        self.company_owners[co] = client_id
        return False

    def _remove_company(self, cid: int) -> bool:
        """Remove company and owner from cache. Returns True if it existed."""
        self.company_owners.pop(cid, None)
        return self.companies.pop(cid, None) is not None

    async def _poll(self, update_type: AdminUpdateType, data: int) -> None:
        """Send raw ADMIN_POLL packet."""
        payload = update_type.value.to_bytes(1, "little") + data.to_bytes(4, "little")
        packet = (3 + len(payload)).to_bytes(2, "little") + b"\x03" + payload
        self.admin._writer.write(packet)
        await self.admin._writer.drain()

    async def _drain(self, timeout: float = 0.5) -> None:
        """Dispatch packets until stream is idle; re-entrancy guard prevents recursion."""
        if self._drain_depth >= 2:
            return
        self._drain_depth += 1
        try:
            while True:
                try:
                    for pkt in await asyncio.wait_for(self.admin.recv(), timeout=timeout):
                        await self.admin.handle_packet(pkt)
                except asyncio.TimeoutError:
                    break
        finally:
            self._drain_depth -= 1

    async def rcon(self, cmd: str, timeout: float = 5,
                   console_wait: str = "", console_timeout: float = 5.0) -> str:
        """Send RCON command; return response text with serialized traffic."""
        if not self.admin:
            raise RuntimeError("rcon called with no active connection")
        self.log.debug(f"RCON> {cmd}")
        buf, buffered = [], []
        async with self._rcon_lock:
            loop = asyncio.get_running_loop()
            await self.admin.send_rcon(cmd)
            deadline = loop.time() + timeout
            done = False
            while not done and (rem := deadline - loop.time()) > 0:
                try:
                    for pkt in await asyncio.wait_for(self.admin.recv(), timeout=rem):
                        if isinstance(pkt, openttdpacket.RconPacket):
                            buf.append(pkt.response.strip())
                        elif isinstance(pkt, openttdpacket.RconEndPacket):
                            done = True
                        else:
                            buffered.append(pkt)
                except asyncio.TimeoutError:
                    break
            if not done:
                raise TimeoutError(f"RCON '{cmd}' timed out")
            result = "\n".join(buf)
            self.log.debug(f"RCON< {result[:200]}")
            cw = console_wait.lower()
            if cw and not any(cw in l.lower() for l in buf):
                found = False
                cw_end = loop.time() + console_timeout
                while not found and (rem := cw_end - loop.time()) > 0:
                    try:
                        pkts = await asyncio.wait_for(self.admin.recv(), timeout=min(rem, 0.5))
                    except asyncio.TimeoutError:
                        break
                    for p in pkts:
                        if isinstance(p, openttdpacket.ConsolePacket) and cw in p.message.lower():
                            found = True
                        else:
                            buffered.append(p)
                if not found:
                    self.log.warning(f"console_wait '{console_wait}' timed out for: {cmd}")
        for pkt in buffered:
            await self.admin.handle_packet(pkt)
        return result

    async def msg(self, text: str, cid: int | None = None) -> None:
        """Send each non-empty line as SERVER_MESSAGE; private to cid or broadcast."""
        if not self.admin or self.admin._writer is None:
            return
        dest = ChatDestTypes.CLIENT if cid is not None else ChatDestTypes.BROADCAST
        for raw in text.split("\n"):
            if line := raw.strip():
                try:
                    await self.admin._chat(line, Actions.SERVER_MESSAGE, dest, cid or 0)
                except Exception as e:
                    self.log.error(f"msg error: {e}")

    async def _snapshot_state(self) -> None:
        """Poll date/clients/companies until a date packet arrives, then drain."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while True:
            await self._poll(AdminUpdateType.DATE, 0)
            await self._poll(AdminUpdateType.CLIENT_INFO, 0xFFFFFFFF)
            for cid in range(16):
                await self._poll(AdminUpdateType.COMPANY_INFO, cid)
            await self._drain(timeout=0.2)
            if self.game_date or loop.time() > deadline:
                break
        await self._drain(timeout=0.5)
        await self._fetch_company_data()

    async def _cancel_tasks(self) -> None:
        """Cancel and await all tracked background tasks."""
        for t in self.tasks: t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _reset_state(self) -> None:
        """Cancel all tasks, flush TCP debris, and clear all game state."""
        await self._cancel_tasks()
        with contextlib.suppress(Exception):
            await self._drain(0.1)
        retry_at = asyncio.get_running_loop().time() + 0.5
        async with self._lock:
            self.companies.clear()
            self.company_owners.clear()
            self.clients.clear()
            self.game_year = self.game_date = 0
            self.is_paused = True
            self.goal_reached = False
            self.reset_pending.clear()
            self.cooldowns.clear()
            self._cleaning.clear()
            self._enforcing.clear()
            self.last_pause_cmd = None
            self.last_cmd_time = 0.0
            self._pause_retry_at = retry_at
        self._client_ready.clear()
        self._drain_depth = 0

    async def _reset_company_1(self) -> None:
        """Delete default company #1 if it is unoccupied and still named 'Unnamed'."""
        async with self._lock:
            co1 = self.companies.get(1)
            if not co1 or co1.name.lower() not in ("", "unnamed") or any(c.company_id == 1 for c in self.clients.values()):
                return
        try:
            await self.rcon("reset_company 1", console_wait=_RCON_CO_DELETED)
            self.log.info("Reset default company #1")
        except Exception as e:
            self.log.debug(f"reset_company 1: {e}")

    async def apply_pause_policy(self) -> None:
        """Pause when no companies exist; unpause on first company."""
        async with self._lock:
            should_pause = not self.companies
            if self.last_pause_cmd == should_pause:
                return
            now = asyncio.get_running_loop().time()
            if not should_pause and now - self.last_cmd_time < 1.0:
                if not self._pause_retry_at:
                    self._pause_retry_at = self.last_cmd_time + 1.1
                return
            self.is_paused = should_pause
            self.last_cmd_time = now
            self.last_pause_cmd = should_pause
        state = "pause" if should_pause else "unpause"
        try:
            resp = await self.rcon(state)
            label = "paused" if should_pause else "unpaused"
            if f"already {label}" not in resp.lower():
                reason = "no companies" if should_pause else "company present"
                self.log.info(f"{label.capitalize()}: {reason}")
        except Exception as e:
            async with self._lock:
                if self.last_pause_cmd == should_pause:
                    self.last_pause_cmd = None
            self.log.error(f"Pause policy error: {e}")
            self._pause_retry_at = asyncio.get_running_loop().time() + 2.0

    async def _poll_loop(self) -> None:
        """Fetch company data at the top of every wall-clock minute (:00s)."""
        while self.running:
            await asyncio.sleep(60 - time.time() % 60)
            if not self.is_paused:
                await self._fetch_company_data()

    async def _fetch_company_data(self) -> None:
        """Fetch 'rcon companies', update cache, then check goal and auto-clean."""
        try:
            resp = await self.rcon("companies")
        except Exception as e:
            self.log.error(f"Company data fetch error: {e}")
            return
        updates = {int(m.group(1)): Company(name=m.group(2), founded=int(m.group(3)), value=int(m.group(4)))
                   for line in resp.splitlines() if (m := _RCON_COMPANY_RE.match(line.strip()))}
        if not updates:
            return
        async with self._lock:
            for cid, data in updates.items():
                if cid not in self.companies:
                    self.log.info(f"Recovered missing company #{cid} '{data.name}' from rcon")
                self.companies[cid] = data
        self.log.debug(f"Company data updated: {sorted(updates)}")
        await self.check_goal()
        await self.auto_clean()

    async def check_goal(self) -> None:
        """Spawn reload countdown if any company reached the goal value."""
        goal = self.cfg["goal"]
        if self.goal_reached or goal <= 0:
            return
        async with self._lock:
            winner = next((d for d in self.companies.values() if d.value >= goal), None)
            if not winner:
                return
            self.goal_reached = True
            winner_name = winner.name
        self.log.info(f"Goal reached: {winner_name} at {fmt(winner.value)}")
        self._spawn(self._do_goal_reload(winner_name, goal))

    async def _do_goal_reload(self, winner_name: str, goal: int) -> None:
        """Announce win, countdown 20s, then load map."""
        try:
            await self.msg(f"========== Congratulations! {winner_name} WINS! ==========\n"
                           f"Company reached {fmt(goal)}")
            for t in (20, 15, 10, 5):
                await self.msg(f"Map reloads in {t}s...")
                await asyncio.sleep(5)
            map_file = self.cfg["map"]
            cmd = _MAP_NEWGAME if not map_file or map_file == _MAP_NEWGAME else (
                f"load_scenario {map_file}" if map_file.endswith(".scn") else f"load {map_file}")
            self._new_game_event.clear()
            await self.rcon(cmd)
            await asyncio.wait_for(self._new_game_event.wait(), timeout=10)
        except Exception as e:
            self.log.error(f"Map reload error: {e}")

    async def auto_clean(self) -> None:
        """Reset old low-value companies. Moves clients first, then resets."""
        age_thresh = self.cfg["clean_age"]
        val_thresh = self.cfg["clean_value"]
        if not age_thresh or not val_thresh:
            return
        async with self._lock:
            pending_cos = {co for co, _ in self.reset_pending.values()}
            to_clean = []
            for cid, d in self.companies.items():
                if (not d.founded or d.value >= val_thresh
                        or cid in self._cleaning or cid in pending_cos
                        or self.game_year - d.founded < age_thresh):
                    continue
                clients = [c for c, cd in self.clients.items() if cd.company_id == cid]
                to_clean.append((cid, d.name, d.value, d.founded, clients))
                self._cleaning.add(cid)
        for cid, name, value, founded, clients in to_clean:
            age = self.game_year - founded
            try:
                for c in clients:
                    await self.rcon(f"move {c} {_SPECTATOR}", console_wait=_RCON_JOINED_SPEC)
                if clients:
                    await asyncio.sleep(0.5)
                await self.rcon(f"reset_company {cid}", console_wait=_RCON_CO_DELETED)
                async with self._lock:
                    self._remove_company(cid)
                self.log.info(f"Auto-clean: co#{cid} '{name}' age={age}yrs val={fmt(value)}")
                await self.msg(f"Company {name} auto-reset")
                await self.apply_pause_policy()
            except Exception as e:
                self.log.error(f"Auto-clean error co#{cid}: {e}")
            finally:
                self._cleaning.discard(cid)

    async def _enforce_limit(self, cid: int, co: int) -> None:
        """Move player to spectator, delete extra company, then notify about limit."""
        async with self._lock:
            if co in self._enforcing:
                return
            self._enforcing.add(co)
            client_name = getattr(self.clients.get(cid), "name", "")
        try:
            await asyncio.sleep(1.0)
            await self.rcon(f"move {cid} {_SPECTATOR}", console_wait=_RCON_JOINED_SPEC)
            await self.rcon(f"reset_company {co}")
            await self.msg(f"Only {self.cfg['max_companies']} companies per client allowed.", cid)
            self.log.info(f"Enforced limit: moved '{client_name}' to spectator, reset co#{co}")
        except Exception as e:
            self.log.error(f"Enforce limit error co#{co}: {e}")
        finally:
            async with self._lock:
                self._enforcing.discard(co)

    async def _get_country(self, ip: str) -> str:
        """Resolve IP to country name via ipapi.co; returns '' for local/unknown. Cached."""
        if not ip or ip in ("127.0.0.1", "0.0.0.0"):
            return ""
        if ip in self._country_cache:
            return self._country_cache[ip]
        try:
            def _fetch():
                with urllib.request.urlopen(f"https://ipapi.co/{ip}/json/", timeout=5) as r:
                    return json.load(r).get("country_name", "") or ""
            country = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except Exception:
            country = ""
        if len(self._country_cache) >= _COUNTRY_CACHE_MAX:
            self._country_cache.pop(next(iter(self._country_cache)))
        self._country_cache[ip] = country
        return country

    async def greet(self, cid: int) -> None:
        """Wait up to 5s for ClientInfo packet then send welcome broadcast + private hint."""
        if event := self._client_ready.pop(cid, None):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=5)
        if not self.running:
            return
        async with self._lock:
            client = self.clients.get(cid)
            if client is None or client.name == _ADMIN_CLIENT_NAME:
                return
            name, ip, paused = client.name, client.ip, self.is_paused
        country = await self._get_country(ip)
        location = f" from {country}" if country else ""
        await self.msg(f"Welcome {name}{location}")
        hint = "create a company to unpause game, type !help for commands" if paused else "type !help for commands"
        await self.msg(hint, cid)

    def _leaderboard(self) -> str:
        """Build company value leaderboard from cached data."""
        if not self.companies:
            return "No companies"
        return "\n".join(["=== Company Value Rankings ==="] +
            [f"{i}. {d.name}: {fmt(d.value)}" for i, d
             in enumerate(sorted(self.companies.values(), key=lambda c: c.value, reverse=True), 1)])

    async def handle_cmd(self, cid: int, text: str) -> None:
        """Dispatch !commands; blocked during pause and subject to per-client cooldown."""
        parts = text.lower().split()
        if not parts:
            return
        async with self._lock:
            if self.is_paused:
                return
            now = asyncio.get_running_loop().time()
            if now - self.cooldowns.get(cid, 0) < _CMD_COOLDOWN:
                return
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < _COOLDOWN_TTL}
            self.cooldowns[cid] = now
        cmd = parts[0]
        self.log.debug(f"Command: !{cmd} from #{cid}")
        try:
            if cmd == "info":
                await self.msg(
                    f"=== Server Info ===\n"
                    f"Goal: first company to reach {fmt(self.cfg['goal'])} wins!\n"
                    f"Gamescript: Production Booster\n"
                    f"Primary industries (coal, wood, oil, etc.)\n"
                    f"Transported >70% increases production, <50% decreases\n"
                    f"Get Admin for your server: github.com/nelbin4/openttd-admin-bot", cid)
            elif cmd == "rules":
                await self.msg(
                    f"=== Game Rules ===\n"
                    f"1. No sabotage, respect other players\n"
                    f"2. No griefing or blocking industries/cities\n"
                    f"3. Do not excessively reserve land\n"
                    f"4. Companies >{self.cfg['clean_age']}yrs & value "
                    f"<{fmt(self.cfg['clean_value'])} auto-cleaned\n"
                    f"5. Only {self.cfg['max_companies']} companies allowed per client", cid)
            elif cmd == "cv":
                await self.msg(self._leaderboard(), cid)
            elif cmd == "reset":
                await self._handle_reset(cid)
            else:
                await self.msg("=== Chat Commands ===\n!info !rules !cv !reset", cid)
        except Exception as e:
            self.log.error(f"Command !{cmd} error: {e}")

    async def _handle_reset(self, cid: int) -> None:
        """Register voluntary company reset; prompt client to move to spectator."""
        token = asyncio.get_running_loop().time()
        async with self._lock:
            co = self.clients.get(cid, Client("", _SPECTATOR)).company_id
            if co != _SPECTATOR:
                self.reset_pending[cid] = (co, token)
        if co == _SPECTATOR:
            await self.msg("You must be in a company", cid)
            return
        self.log.info(f"Reset request: client #{cid} co#{co}")
        await self.msg(f"Move to spectator in {_RESET_TIMEOUT}s to reset company", cid)

        async def _timeout(tok: float) -> None:
            await asyncio.sleep(_RESET_TIMEOUT)
            async with self._lock:
                if self.reset_pending.get(cid, (None, None))[1] != tok:
                    return
                self.reset_pending.pop(cid, None)
            await self.msg(f"Reset timeout after {_RESET_TIMEOUT}s", cid)
        self._spawn(_timeout(token))

    def _setup_handlers(self) -> None:
        @self.admin.add_handler(openttdpacket.ConsolePacket)
        async def on_console(admin: Admin, pkt: openttdpacket.ConsolePacket):
            msg = pkt.message.strip().lower()
            self.log.debug(f"[Console] {msg}")
            if "game paused" in msg:
                self.is_paused = True
            elif "unpaused" in msg:
                self.is_paused = False

        @self.admin.add_handler(openttdpacket.ChatPacket)
        async def on_chat(admin: Admin, pkt: openttdpacket.ChatPacket):
            msg = pkt.message.strip()
            if msg.startswith("!") and (client_id := getattr(pkt, "id", None)) is not None:
                await self.handle_cmd(client_id, msg[1:])

        @self.admin.add_handler(openttdpacket.ClientJoinPacket)
        async def on_join(admin: Admin, pkt: openttdpacket.ClientJoinPacket):
            self._client_ready[pkt.id] = asyncio.Event()
            self._spawn(self.greet(pkt.id))

        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        async def on_client_info(admin: Admin, pkt: openttdpacket.ClientInfoPacket):
            co = self._pkt_cid(pkt)
            async with self._lock:
                self.clients[pkt.id] = Client(name=pkt.name, company_id=co, ip=getattr(pkt, "ip", "0.0.0.0"))
                enforce_limit = self._check_ownership(pkt.id, co)
            self.log.debug(f"ClientInfo: #{pkt.id} '{pkt.name}' co={co}")
            if enforce_limit:
                self.log.warning(f"Client #{pkt.id} exceeded limit on ClientInfo, co#{co}")
                self._spawn(self._enforce_limit(pkt.id, co))
            if event := self._client_ready.get(pkt.id):
                event.set()

        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        async def on_client_update(admin: Admin, pkt: openttdpacket.ClientUpdatePacket):
            co = self._pkt_cid(pkt)
            enforce_limit = False
            pending_co = None
            async with self._lock:
                client = self.clients.get(pkt.id)
                if client:
                    client.name = pkt.name
                    client.company_id = co
                else:
                    self.clients[pkt.id] = Client(name=pkt.name, company_id=co)
                enforce_limit = self._check_ownership(pkt.id, co)
                if co == _SPECTATOR:
                    if pending := self.reset_pending.pop(pkt.id, None):
                        pending_co, _ = pending
            if enforce_limit:
                self.log.warning(f"Client #{pkt.id} exceeded limit, removing co#{co}")
                self._spawn(self._enforce_limit(pkt.id, co))
                return
            if pending_co is not None:
                await self.rcon(f"reset_company {pending_co}", console_wait=_RCON_CO_DELETED)
                await self.msg(f"Company #{pending_co} reset", pkt.id)
                async with self._lock:
                    self._remove_company(pending_co)
                self.log.info(f"Reset complete: co#{pending_co}")
                await self.apply_pause_policy()

        @self.admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        async def on_client_disconnect(admin: Admin, pkt: Any):
            if (client_id := getattr(pkt, "id", None)) is None:
                return
            if isinstance(pkt, openttdpacket.ClientErrorPacket):
                self.log.debug(f"[ClientError] #{client_id}: {getattr(pkt, 'error', '?')}")
            async with self._lock:
                self.clients.pop(client_id, None)
                self.reset_pending.pop(client_id, None)
                self.cooldowns.pop(client_id, None)
                self.company_owners = {co: oid for co, oid in self.company_owners.items() if oid != client_id}
            if event := self._client_ready.pop(client_id, None):
                event.set()
            self.log.debug(f"Client #{client_id} disconnected")

        @self.admin.add_handler(openttdpacket.CompanyInfoPacket, openttdpacket.CompanyNewPacket, openttdpacket.CompanyUpdatePacket)
        async def on_company(admin: Admin, pkt: Any):
            cid = self._to_cid(pkt.id)
            name = getattr(pkt, "name", None)
            founded = pkt.year if isinstance(getattr(pkt, "year", None), int) else None
            async with self._lock:
                if co := self.companies.get(cid):
                    if name: co.name = name
                    if founded is not None: co.founded = founded
                    return
                self.companies[cid] = Company(name=name or "", founded=founded or 0)
            self.log.info(f"Company added: #{cid} '{name or 'Unnamed'}'")
            await self.apply_pause_policy()

        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin: Admin, pkt: openttdpacket.CompanyRemovePacket):
            cid = self._to_cid(pkt.id)
            async with self._lock:
                removed = self._remove_company(cid)
            if removed:
                self.log.info(f"Company removed: #{cid}")
                await self.apply_pause_policy()

        @self.admin.add_handler(openttdpacket.DatePacket)
        async def on_date(admin: Admin, pkt: openttdpacket.DatePacket):
            if pkt.date == self.game_date:
                return
            d = date(1, 1, 1) + timedelta(days=pkt.date)
            self.game_date = pkt.date
            new_year = d.year - 1
            if new_year != self.game_year:
                self.game_year = new_year
            self.log.debug(f"Date: {d.month:02d}-{d.day:02d}-{d.year - 1}")

        @self.admin.add_handler(openttdpacket.NewGamePacket)
        async def on_new_game(admin: Admin, pkt: openttdpacket.NewGamePacket):
            self.log.info("New game detected")
            await self._reset_state()
            await self._snapshot_state()
            await asyncio.sleep(1.0)
            await self._reset_company_1()
            await self.apply_pause_policy()
            self._spawn(self._poll_loop())
            self._new_game_event.set()

        @self.admin.add_handler(openttdpacket.ShutdownPacket)
        async def on_shutdown(admin: Admin, pkt: openttdpacket.ShutdownPacket):
            self.running = False
            self.log.info("Server shutdown")

    async def cleanup(self) -> None:
        """Cancel all tasks; Admin connection is closed by async-with in run()."""
        self.running = False
        await self._cancel_tasks()
        self.admin = None

    async def run(self) -> None:
        """Connect, subscribe, snapshot state, then drive recv loop with background polling."""
        try:
            async with Admin(ip=self.cfg["ip"], port=self.cfg["port"]) as admin:
                self.admin = admin
                await admin.login(self.cfg["admin_name"], self.cfg["admin_pass"])
                for update, freq in (
                    (AdminUpdateType.CHAT,         AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.CLIENT_INFO,  AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.CONSOLE,      AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.DATE,         AdminUpdateFrequency.MONTHLY),
                ):
                    await admin.subscribe(update, freq)
                self._setup_handlers()
                await self._snapshot_state()
                await self._reset_company_1()
                await self.apply_pause_policy()
                self.running = True
                self.log.info(f"Connected to {self.cfg['ip']}:{self.cfg['port']}")
                await self.msg("Admin connected")
                self._spawn(self._poll_loop())
                loop = asyncio.get_running_loop()
                bcast_iv = self.cfg["broadcast_cv"]
                t = time.time()
                next_broadcast = t + (bcast_iv - t % bcast_iv)
                while self.running:
                    try:
                        async with self._rcon_lock:
                            packets = await asyncio.wait_for(admin.recv(), timeout=_RECV_TIMEOUT)
                    except asyncio.TimeoutError:
                        packets = []
                    if not packets and admin._reader and admin._reader.at_eof():
                        raise ConnectionError("Admin connection closed (EOF)")
                    for pkt in packets:
                        await admin.handle_packet(pkt)
                    now = loop.time()
                    wall_now = time.time()
                    if self._pause_retry_at and now >= self._pause_retry_at:
                        self._pause_retry_at = 0.0
                        await self.apply_pause_policy()
                    if not self.is_paused and wall_now >= next_broadcast:
                        await self.msg(self._leaderboard())
                        self.log.info("Broadcast leaderboard")
                        t = time.time()
                        next_broadcast = t + (bcast_iv - t % bcast_iv)
        except Exception as e:
            if not isinstance(e, (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError)):
                self.log.error(f"Bot run error: {e}", exc_info=True)
            raise
        finally:
            await self.cleanup()

async def run_bot(cfg: dict[str, Any], log: logging.Logger) -> None:
    """Run bot with automatic reconnect on connection failures."""
    addr = f"{cfg['ip']}:{cfg['port']}"
    while True:
        try:
            await Bot(cfg, log).run()
            log.info(f"[{addr}] disconnected, reconnecting in {_RECONNECT_DELAY}s...")
        except (ConnectionError, OSError, TimeoutError):
            log.warning(f"[{addr}] unavailable, retrying in {_RECONNECT_DELAY}s...")
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
        await asyncio.sleep(_RECONNECT_DELAY)

async def main() -> None:
    """Load settings.cfg, configure logging, launch one bot task per server."""
    signal.signal(signal.SIGINT,  lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        servers = load_config("settings.cfg")
    except Exception as e:
        print(f"Error loading settings.cfg: {e}")
        return
    if not servers:
        print("No servers configured in settings.cfg")
        return
    log_level = logging.DEBUG if any(s.get("debug") for s in servers) else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("[Main]")
    log.info("=== OpenTTD Admin Bot Starting ===")
    valid = []
    for cfg in servers:
        if errs := normalize_config(cfg, log):
            log.error(f"Config error [{cfg.get('name', '?')}]: {errs[0]}")
        else:
            valid.append(cfg)
    if not valid:
        log.error("No valid servers; exiting.")
        return
    log.info(f"Starting {len(valid)} bot instance(s)")
    await asyncio.gather(*(run_bot(c, logging.getLogger(f"[{c['ip']}:{c['port']}]")) for c in valid))

if __name__ == "__main__":
    asyncio.run(main())
