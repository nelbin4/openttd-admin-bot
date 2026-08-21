from __future__ import annotations

import asyncio
import configparser
import contextlib
import ipaddress
import json
import logging
import os
import re
import signal
import time
import urllib.error
import urllib.request
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, TypedDict, cast

from aiopyopenttdadmin import (
    Admin,
    AdminUpdateFrequency,
    AdminUpdateType,
    openttdpacket,
)
from pyopenttdadmin.enums import Actions, ChatDestTypes

_RCON_COMPANY_RE = re.compile(
    r"#:(\d+)\([^)]+\)\s+Company Name:\s+'(.*)'\s+Year Founded:\s+(\d+).*?Value:\s+(\d+)"
)

_RECONNECT_DELAY = 30  # initial seconds between reconnect attempts
_RECONNECT_MAX_DELAY = 300
_GOAL_RELOAD_MAX_RETRIES = 3  # give up re-announcing/retrying after this many failed reloads
_CMD_COOLDOWN = 1.0  # minimum seconds between !commands per client
_RESET_TIMEOUT = 15  # seconds for voluntary company reset confirmation
_SPECTATOR = 255  # company ID sentinel for spectators
_RCON_JOINED_SPEC = "has joined spectators"
_RCON_CO_DELETED = "Company deleted"
_MAP_NEWGAME = "newgame"
_COOLDOWN_TTL = 300  # seconds before idle cooldown entries are evicted
_MAX_DISPLAY_TEXT = 80
_MAX_CHAT_LINE = 200
_CONSOLE_PAUSED = "game paused"
_CONSOLE_UNPAUSED = "unpaused"
_RESP_ALREADY_PAUSED = "already paused"
_RESP_ALREADY_UNPAUSED = "already unpaused"


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


class _PollPacket(openttdpacket.Packet):
    """Admin poll packet routed through the library's authenticated send path."""

    packet_type = openttdpacket.PacketType.ADMIN_POLL

    def __init__(self, update_type: AdminUpdateType, data: int) -> None:
        self._payload: bytes = update_type.value.to_bytes(1, "little") + data.to_bytes(4, "little")

    def to_bytes(self) -> bytes:
        return self._payload


class ServerConfig(TypedDict):
    name: str
    ip: str
    port: int
    admin_name: str
    admin_pass: str
    debug: bool
    map: str
    goal: int
    clean_age: int
    clean_value: int
    max_companies: int
    broadcast_cv: int
    geo_lookup: bool


def clean_display_text(value: Any, limit: int = _MAX_DISPLAY_TEXT) -> str:
    """Remove control characters before names reach logs or chat."""
    if value is None:
        return ""
    printable = "".join(char if char.isprintable() else " " for char in str(value))
    return " ".join(printable.split())[:limit]


class AdminTransport:
    """Small compatibility adapter for the admin library's wire operations."""

    def __init__(self, admin: Admin) -> None:
        self.admin = admin

    @property
    def at_eof(self) -> bool:
        return bool(self.admin._reader and self.admin._reader.at_eof())

    async def poll(self, update_type: AdminUpdateType, data: int) -> None:
        packet = _PollPacket(update_type, data)
        await self.admin._send(packet)

    async def server_message(
        self, message: str, destination: ChatDestTypes, client_id: int
    ) -> None:
        await self.admin._chat(message, Actions.SERVER_MESSAGE, destination, client_id)


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

    def _parse(section: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": section}
        for key, val in cfg.items(section):
            low = val.lower()
            entry[key] = (
                int(val) if val.isdigit() else (low == "true") if low in ("true", "false") else val
            )
        return entry

    return [_parse(s) for s in cfg.sections()]


def normalize_config(cfg: dict[str, Any], log: logging.Logger) -> list[str]:
    """Validate and normalize config in-place; return fatal error strings."""
    # OPENTTD_ADMIN_PASS_<SECTION> lets a deployment inject the password via
    # env var / Docker secret instead of plaintext in settings.cfg.
    env_key = "OPENTTD_ADMIN_PASS_" + re.sub(r"[^A-Za-z0-9]", "_", str(cfg.get("name", ""))).upper()
    if env_pass := os.environ.get(env_key):
        cfg["admin_pass"] = env_pass
    for field in ("ip", "port", "admin_name", "admin_pass"):
        if not cfg.get(field):
            return [f"Missing required field: {field}"]
    errors: list[str] = []
    if not isinstance(cfg.get("port"), int) or not 1 <= cfg["port"] <= 65535:
        errors.append(f"Invalid port: {cfg.get('port')}")
    cfg.setdefault("debug", False)
    cfg.setdefault("map", "")
    cfg.setdefault("geo_lookup", False)  # off by default: sends player IPs to ipapi.co
    max_co = cfg.get("max_companies", 2)
    if not isinstance(max_co, int) or not 1 <= max_co <= 64:
        errors.append(f"Invalid max_companies: {max_co!r} (expected 1-64)")
    cfg["max_companies"] = max_co
    bcast = cfg.get("broadcast_cv", 3600)
    if not isinstance(bcast, int) or not 1 <= bcast <= 86400:
        errors.append(f"Invalid broadcast_cv: {bcast!r} (expected 1-86400 seconds)")
    cfg["broadcast_cv"] = bcast
    goal = cfg.get("goal", 0)
    if not isinstance(goal, int) or goal < 0:
        errors.append(f"Invalid goal: {goal!r} (expected a non-negative integer)")
    cfg["goal"] = goal
    clean_age = cfg.get("clean_age", 0)
    clean_value = cfg.get("clean_value", 0)
    if not isinstance(clean_age, int) or clean_age < 0:
        errors.append(f"Invalid clean_age: {clean_age!r} (expected zero or positive)")
    if not isinstance(clean_value, int) or clean_value < 0:
        errors.append(f"Invalid clean_value: {clean_value!r} (expected zero or positive)")
    if bool(clean_age) != bool(clean_value):
        errors.append("clean_age and clean_value must both be zero or both be positive")
    cfg["clean_age"] = clean_age
    cfg["clean_value"] = clean_value
    map_file = cfg.get("map", "")
    if not isinstance(map_file, str) or (
        map_file
        and map_file != _MAP_NEWGAME
        and not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:scn|sav)", map_file)
    ):
        errors.append(f"Invalid map filename: {map_file!r}")
    if errors:
        return errors
    if not cfg["clean_age"] or not cfg["clean_value"]:
        log.info("auto-clean disabled")
    log.info(f"[{cfg['name']}] goal:{cfg['goal']} map:{cfg['map'] or 'disabled'}")
    return []


class Bot:
    """OpenTTD admin bot: enforces company limits, manages pause, goal, and auto-clean."""

    def __init__(self, cfg: ServerConfig, log: logging.Logger) -> None:
        self.cfg, self.log = cfg, log
        self.admin: Admin | None = None
        self.transport: AdminTransport | None = None
        self.running = False
        self._lock = asyncio.Lock()
        self.companies: dict[int, Company] = {}
        self.company_owners: dict[int, int] = {}
        self.owner_counts: dict[int, int] = {}
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
        self._country_sem = asyncio.Semaphore(8)
        self._drain_depth = 0
        self._last_cooldown_gc = 0.0
        self._parse_failures = 0
        self._goal_reload_failures = 0

    @staticmethod
    def _to_cid(raw: int) -> int:
        """0-based → 1-based company ID; spectator unchanged."""
        return _SPECTATOR if raw == _SPECTATOR else raw + 1

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """Track a fire-and-forget task and report unexpected failures."""
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            return
        if error := task.exception():
            self.log.error(
                "Background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _pkt_cid(self, pkt: Any) -> int:
        """Extract 1-based company ID from a client packet."""
        return self._to_cid(getattr(pkt, "play_as", getattr(pkt, "company_id", _SPECTATOR)))

    def _check_ownership(self, client_id: int, co: int) -> bool:
        """Assign ownership and report whether the client exceeded its limit."""
        if (
            co == _SPECTATOR
            or co not in self.companies
            or co in self._enforcing
            or co in self.company_owners
        ):
            return False
        if self.owner_counts.get(client_id, 0) >= self.cfg["max_companies"]:
            return True
        self.company_owners[co] = client_id
        self.owner_counts[client_id] = self.owner_counts.get(client_id, 0) + 1
        return False

    def _remove_company(self, cid: int) -> bool:
        """Remove company and owner from cache. Returns True if it existed."""
        owner = self.company_owners.pop(cid, None)
        if owner is not None:
            remaining = self.owner_counts.get(owner, 0) - 1
            if remaining > 0:
                self.owner_counts[owner] = remaining
            else:
                self.owner_counts.pop(owner, None)
        return self.companies.pop(cid, None) is not None

    def _upsert_client(self, pkt_id: int, name: str, co: int, ip: str | None = None) -> bool:
        """Update a client and report whether the company limit was exceeded."""
        client = self.clients.get(pkt_id)
        if client:
            client.name = name
            client.company_id = co
            if ip is not None:
                client.ip = ip
        else:
            self.clients[pkt_id] = Client(name=name, company_id=co, ip=ip or "0.0.0.0")
        return self._check_ownership(pkt_id, co)

    async def _init_game_state(self, delay_reset: float = 0) -> None:
        """Snapshot state, reset default company, apply pause policy, start poll loop."""
        await self._snapshot_state()
        if delay_reset:
            await asyncio.sleep(delay_reset)
        await self._reset_company_1()
        await self.apply_pause_policy()
        self._spawn(self._poll_loop())
        self._spawn(self._maintenance_loop())

    async def _poll(self, update_type: AdminUpdateType, data: int) -> None:
        """Send raw ADMIN_POLL packet."""
        transport = self.transport
        if transport is None:
            raise RuntimeError("poll called with no active connection")
        await transport.poll(update_type, data)

    async def _drain(self, timeout: float = 0.5) -> None:
        """Dispatch packets until stream is idle.

        Re-entrancy guard: nothing in this file currently calls _drain()
        from within a packet handler it would itself invoke, but the depth
        cap protects against that if it's ever introduced (depth 1 = a
        normal top-level call already in progress; depth 2 blocks a second
        nested call rather than recursing unboundedly).
        """
        if self._drain_depth >= 2:
            return
        self._drain_depth += 1
        try:
            packets = []
            admin = self.admin
            if admin is None:
                raise RuntimeError("drain called with no active connection")
            while True:
                try:
                    async with self._rcon_lock:
                        received = await asyncio.wait_for(admin.recv(), timeout=timeout)
                    packets.extend(received)
                except TimeoutError:
                    break
            for pkt in packets:
                await admin.handle_packet(pkt)
        finally:
            self._drain_depth -= 1

    async def rcon(
        self,
        cmd: str,
        timeout: float = 5,
        console_wait: str = "",
        console_timeout: float = 5.0,
    ) -> str:
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
                except TimeoutError:
                    break
            if not done:
                raise TimeoutError(f"RCON '{cmd}' timed out")
            result = "\n".join(buf)
            self.log.debug(f"RCON< {result[:200]}")
            cw = console_wait.lower()
            if cw and not any(cw in line.lower() for line in buf):
                found = False
                cw_end = loop.time() + console_timeout
                while not found and (rem := cw_end - loop.time()) > 0:
                    try:
                        pkts = await asyncio.wait_for(self.admin.recv(), timeout=min(rem, 0.5))
                    except TimeoutError:
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
        if not self.admin:
            return
        transport = self.transport
        if transport is None:
            return
        dest = ChatDestTypes.CLIENT if cid is not None else ChatDestTypes.BROADCAST
        for raw in text.split("\n"):
            if line := clean_display_text(raw, _MAX_CHAT_LINE):
                try:
                    await transport.server_message(line, dest, cid or 0)
                except Exception as e:
                    self.log.error(f"msg error: {e}")

    async def _snapshot_state(self) -> None:
        """Poll clients/companies once, then poll date until it arrives."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        await self._poll(AdminUpdateType.CLIENT_INFO, 0xFFFFFFFF)
        for cid in range(15):
            await self._poll(AdminUpdateType.COMPANY_INFO, cid)
        while True:
            await self._poll(AdminUpdateType.DATE, 0)
            await self._drain(timeout=0.2)
            if self.game_date or loop.time() > deadline:
                break
        await self._drain(timeout=0.5)
        await self._fetch_company_data()

    async def _cancel_tasks(self) -> None:
        """Cancel and await all tracked background tasks."""
        for t in self.tasks:
            t.cancel()
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
            self.owner_counts.clear()
            self.clients.clear()
            self.game_year = self.game_date = 0
            self.is_paused = True
            self.goal_reached = False
            self._goal_reload_failures = 0
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
            if (
                not co1
                or co1.name.lower() not in ("", "unnamed")
                or any(c.company_id == 1 for c in self.clients.values())
            ):
                return
        try:
            await self.rcon("reset_company 1", console_wait=_RCON_CO_DELETED)
            self.log.info("Reset default company #1")
        except Exception as e:
            self.log.error(f"reset_company 1 failed: {e}")

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
            already = _RESP_ALREADY_PAUSED if should_pause else _RESP_ALREADY_UNPAUSED
            if already not in resp.lower():
                reason = "no companies" if should_pause else "company present"
                self.log.info(f"{state.capitalize()}d: {reason}")
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

    async def _maintenance_loop(self) -> None:
        """Retry pause policy and broadcast the leaderboard on a fixed cadence.

        Runs independently of the main recv loop so these checks aren't delayed
        by however long an in-flight RCON call holds `_rcon_lock`.
        """
        bcast_iv = self.cfg["broadcast_cv"]

        def _next_bcast() -> float:
            t = time.time()
            return t + (bcast_iv - t % bcast_iv)

        next_broadcast = _next_bcast()
        loop = asyncio.get_running_loop()
        while self.running:
            await asyncio.sleep(1.0)
            if self._pause_retry_at and loop.time() >= self._pause_retry_at:
                self._pause_retry_at = 0.0
                await self.apply_pause_policy()
            if not self.is_paused and time.time() >= next_broadcast:
                await self.msg(self._leaderboard())
                self.log.info("Broadcast leaderboard")
                next_broadcast = _next_bcast()

    async def _fetch_company_data(self) -> None:
        """Fetch 'rcon companies', update cache, then check goal and auto-clean."""
        try:
            resp = await self.rcon("companies")
        except Exception as e:
            self.log.error(f"Company data fetch error: {e}")
            return
        updates = {
            int(m.group(1)): Company(
                name=clean_display_text(m.group(2)), founded=int(m.group(3)), value=int(m.group(4))
            )
            for line in resp.splitlines()
            if (m := _RCON_COMPANY_RE.match(line.strip()))
        }
        if not updates:
            if resp.strip():
                self._parse_failures += 1
                # Log loudly on the 1st and 5th consecutive failure (fast
                # signal that this is a real, ongoing problem, not a blip),
                # then remind every 30 minutes rather than spamming every
                # single minute forever.
                if self._parse_failures in (1, 5) or self._parse_failures % 30 == 0:
                    self.log.error(
                        f"rcon 'companies' returned no parseable lines for "
                        f"{self._parse_failures} consecutive poll(s) — goal "
                        f"detection and auto-clean are non-functional until "
                        f"this is fixed. Check the OpenTTD server's version/"
                        f"locale against this bot's expected output format. "
                        f"Last response: {resp[:200]!r}"
                    )
            return
        self._parse_failures = 0
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
            await self.msg(
                f"========== Congratulations! {winner_name} WINS! ==========\n"
                f"Company reached {fmt(goal)}"
            )
            for t in (20, 15, 10, 5):
                await self.msg(f"Map reloads in {t}s...")
                await asyncio.sleep(5)
            map_file = self.cfg["map"]
            cmd = (
                _MAP_NEWGAME
                if not map_file or map_file == _MAP_NEWGAME
                else (
                    f"load_scenario {map_file}" if map_file.endswith(".scn") else f"load {map_file}"
                )
            )
            self._new_game_event.clear()
            await self.rcon(cmd)
        except Exception as e:
            # The reload command itself never got through — nothing is now in
            # progress server-side. Retry a bounded number of times rather
            # than forever, so a persistently broken map config doesn't
            # re-announce and re-attempt indefinitely.
            self.log.error(f"Map reload error: {e}")
            self._goal_reload_failed()
            return
        try:
            await asyncio.wait_for(self._new_game_event.wait(), timeout=10)
        except TimeoutError:
            # The command was accepted, but some maps genuinely take a while
            # to load — give it a longer grace period before concluding the
            # reload silently failed server-side (bad/missing file, etc.).
            try:
                await asyncio.wait_for(self._new_game_event.wait(), timeout=50)
            except TimeoutError:
                self.log.error(
                    "Map reload: no newgame observed after 60s total; "
                    "assuming the reload failed"
                )
                self._goal_reload_failed()
                return
        self._goal_reload_failures = 0

    def _goal_reload_failed(self) -> None:
        """Allow a retry on the next poll tick, up to a bounded number of
        consecutive failures. Beyond that, give up permanently for this
        connection rather than re-announcing/re-attempting every ~90s
        forever — a persistently broken map config needs a human to fix it.
        """
        self._goal_reload_failures += 1
        if self._goal_reload_failures >= _GOAL_RELOAD_MAX_RETRIES:
            self.log.error(
                f"Map reload failed {self._goal_reload_failures} consecutive "
                f"times — giving up. The win will not be re-announced or "
                f"retried until this bot reconnects or restarts. Check that "
                f"'map' in settings.cfg points to a file that actually "
                f"exists on the server."
            )
            return
        self.goal_reached = False

    async def auto_clean(self) -> None:
        """Reset old low-value companies. Moves clients first, then resets."""
        age_thresh = self.cfg["clean_age"]
        val_thresh = self.cfg["clean_value"]
        if not age_thresh or not val_thresh:
            return
        async with self._lock:
            pending_cos = {co for co, _ in self.reset_pending.values()}
            co_clients: dict[int, list[int]] = {}
            for c, cd in self.clients.items():
                co_clients.setdefault(cd.company_id, []).append(c)
            to_clean = []
            for cid, d in self.companies.items():
                if (
                    not d.founded
                    or d.value >= val_thresh
                    or cid in self._cleaning
                    or cid in pending_cos
                    or self.game_year - d.founded < age_thresh
                ):
                    continue
                to_clean.append((cid, d.name, d.value, d.founded, co_clients.get(cid, [])))
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

    async def _lookup_country(self, ip: str) -> str:
        """Query ipapi.co directly for an IP's country; returns '' on any failure.

        No caching or storage — every call is a live HTTP request, bounded by a
        semaphore so a burst of joins can't fire unlimited concurrent lookups.
        """
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        if not address.is_global:
            return ""
        loop = asyncio.get_running_loop()
        try:
            async with self._country_sem:

                def _fetch() -> str:
                    request = urllib.request.Request(
                        f"https://ipapi.co/{ip}/json/",
                        headers={"User-Agent": "openttd-admin/1.0"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as r:
                        return json.load(r).get("country_name", "") or ""

                return await loop.run_in_executor(None, _fetch)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            self.log.debug(f"Country lookup failed for {ip}: {e}")
        except Exception as e:
            self.log.error(f"Country lookup error for {ip}: {e}")
        return ""

    async def greet(self, cid: int) -> None:
        """Wait up to 5s for ClientInfo packet, delay greeting 5s, then welcome."""
        if event := self._client_ready.pop(cid, None):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=5)
        if not self.running:
            return
        async with self._lock:
            client = self.clients.get(cid)
            if client is None or client.name == "Admin":  # admin login itself; skip greeting
                return
            name, ip, _ = client.name, client.ip, self.is_paused
        await asyncio.sleep(5)
        if not self.running:
            return
        country = await self._lookup_country(ip) if self.cfg.get("geo_lookup") else ""
        location = f" from {country}" if country else ""
        await self.msg(f"Welcome {name}{location}")
        hint = (
            "create a company to unpause game, type !help for commands"
            if self.is_paused
            else "type !help for commands"
        )
        await self.msg(hint, cid)

    def _leaderboard(self) -> str:
        """Build company value leaderboard from cached data."""
        if not self.companies:
            return "No companies"
        return "\n".join(
            ["=== Company Value Rankings ==="]
            + [
                f"{i}. {d.name}: {fmt(d.value)}"
                for i, d in enumerate(
                    sorted(self.companies.values(), key=lambda c: c.value, reverse=True),
                    1,
                )
            ]
        )

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
            if now - self._last_cooldown_gc > _COOLDOWN_TTL:
                self._last_cooldown_gc = now
                self.cooldowns = {
                    k: v for k, v in self.cooldowns.items() if now - v < _COOLDOWN_TTL
                }
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
                    f"Get Admin for your server: github.com/nelbin4/openttd-admin-bot",
                    cid,
                )
            elif cmd == "rules":
                clean_rule = (
                    f"4. Companies >{self.cfg['clean_age']}yrs & value "
                    f"<{fmt(self.cfg['clean_value'])} auto-cleaned\n"
                    if self.cfg["clean_age"]
                    else "4. Auto-clean is disabled on this server\n"
                )
                await self.msg(
                    f"=== Game Rules ===\n"
                    f"1. No sabotage, respect other players\n"
                    f"2. No griefing or blocking industries/cities\n"
                    f"3. Do not excessively reserve land\n"
                    f"{clean_rule}"
                    f"5. Only {self.cfg['max_companies']} companies allowed per client",
                    cid,
                )
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
        admin = self.admin
        if admin is None:
            raise RuntimeError("handler setup called with no active connection")

        @admin.add_handler(openttdpacket.ConsolePacket)
        async def on_console(admin: Admin, pkt: openttdpacket.ConsolePacket) -> None:
            msg = pkt.message.strip().lower()
            self.log.debug(f"[Console] {msg}")
            if _CONSOLE_PAUSED in msg:
                async with self._lock:
                    self.is_paused = True
                    self.last_pause_cmd = True
            elif _CONSOLE_UNPAUSED in msg:
                async with self._lock:
                    self.is_paused = False
                    self.last_pause_cmd = False

        @admin.add_handler(openttdpacket.ChatPacket)
        async def on_chat(admin: Admin, pkt: openttdpacket.ChatPacket) -> None:
            msg = pkt.message.strip()
            if msg.startswith("!") and (client_id := getattr(pkt, "id", None)) is not None:
                await self.handle_cmd(client_id, msg[1:])

        @admin.add_handler(openttdpacket.ClientJoinPacket)
        async def on_join(admin: Admin, pkt: openttdpacket.ClientJoinPacket) -> None:
            self._client_ready[pkt.id] = asyncio.Event()
            self._spawn(self.greet(pkt.id))

        @admin.add_handler(openttdpacket.ClientInfoPacket)
        async def on_client_info(admin: Admin, pkt: openttdpacket.ClientInfoPacket) -> None:
            co = self._pkt_cid(pkt)
            name = clean_display_text(pkt.name)
            async with self._lock:
                enforce_limit = self._upsert_client(
                    pkt.id, name, co, ip=getattr(pkt, "ip", "0.0.0.0")
                )
            self.log.debug(f"ClientInfo: #{pkt.id} '{name}' co={co}")
            if enforce_limit:
                self.log.warning(f"Client #{pkt.id} exceeded limit, co#{co}")
                self._spawn(self._enforce_limit(pkt.id, co))
            if event := self._client_ready.get(pkt.id):
                event.set()

        @admin.add_handler(openttdpacket.ClientUpdatePacket)
        async def on_client_update(admin: Admin, pkt: openttdpacket.ClientUpdatePacket) -> None:
            co = self._pkt_cid(pkt)
            pending_co = None
            async with self._lock:
                enforce_limit = self._upsert_client(pkt.id, clean_display_text(pkt.name), co)
                if co == _SPECTATOR:
                    if pending := self.reset_pending.pop(pkt.id, None):
                        pending_co, _ = pending
            if enforce_limit:
                self.log.warning(f"Client #{pkt.id} exceeded limit, co#{co}")
                self._spawn(self._enforce_limit(pkt.id, co))
                return
            if pending_co is not None:
                try:
                    await self.rcon(f"reset_company {pending_co}", console_wait=_RCON_CO_DELETED)
                    await self.msg(f"Company #{pending_co} reset", pkt.id)
                    async with self._lock:
                        self._remove_company(pending_co)
                    self.log.info(f"Reset complete: co#{pending_co}")
                    await self.apply_pause_policy()
                except Exception as e:
                    self.log.error(f"Reset error co#{pending_co}: {e}")

        @admin.add_handler(openttdpacket.ClientQuitPacket, openttdpacket.ClientErrorPacket)
        async def on_client_disconnect(admin: Admin, pkt: Any) -> None:
            if (client_id := getattr(pkt, "id", None)) is None:
                return
            if isinstance(pkt, openttdpacket.ClientErrorPacket):
                self.log.debug(f"[ClientError] #{client_id}: {getattr(pkt, 'error', '?')}")
            async with self._lock:
                self.clients.pop(client_id, None)
                self.reset_pending.pop(client_id, None)
                self.cooldowns.pop(client_id, None)
                owned = [co for co, oid in self.company_owners.items() if oid == client_id]
                for co in owned:
                    del self.company_owners[co]
                self.owner_counts.pop(client_id, None)
            if event := self._client_ready.pop(client_id, None):
                event.set()
            self.log.debug(f"Client #{client_id} disconnected")

        @admin.add_handler(
            openttdpacket.CompanyInfoPacket,
            openttdpacket.CompanyNewPacket,
            openttdpacket.CompanyUpdatePacket,
        )
        async def on_company(admin: Admin, pkt: Any) -> None:
            cid = self._to_cid(pkt.id)
            name = clean_display_text(getattr(pkt, "name", ""))
            founded = pkt.year if isinstance(getattr(pkt, "year", None), int) else None
            async with self._lock:
                if co := self.companies.get(cid):
                    if name:
                        co.name = name
                    if founded is not None:
                        co.founded = founded
                    return
                self.companies[cid] = Company(name=name or "", founded=founded or 0)
            self.log.info(f"Company added: #{cid} '{name or 'Unnamed'}'")
            await self.apply_pause_policy()

        @admin.add_handler(openttdpacket.CompanyRemovePacket)
        async def on_company_remove(admin: Admin, pkt: openttdpacket.CompanyRemovePacket) -> None:
            cid = self._to_cid(pkt.id)
            async with self._lock:
                removed = self._remove_company(cid)
            if removed:
                self.log.info(f"Company removed: #{cid}")
                await self.apply_pause_policy()

        @admin.add_handler(openttdpacket.DatePacket)
        async def on_date(admin: Admin, pkt: openttdpacket.DatePacket) -> None:
            if pkt.date == self.game_date:
                return
            d = date(1, 1, 1) + timedelta(days=pkt.date)
            self.game_date = pkt.date
            new_year = d.year - 1
            if new_year != self.game_year:
                self.game_year = new_year
            self.log.debug(f"Date: {d.month:02d}-{d.day:02d}-{d.year - 1}")

        @admin.add_handler(openttdpacket.NewGamePacket)
        async def on_new_game(admin: Admin, pkt: openttdpacket.NewGamePacket) -> None:
            self.log.info("New game detected")
            await self._reset_state()
            await self._init_game_state(delay_reset=1.0)
            self._new_game_event.set()

        @admin.add_handler(openttdpacket.ShutdownPacket)
        async def on_shutdown(admin: Admin, pkt: openttdpacket.ShutdownPacket) -> None:
            self.running = False
            self.log.info("Server shutdown")

    async def cleanup(self) -> None:
        """Cancel all tasks; Admin connection closed by async-with in run()."""
        self.running = False
        await self._cancel_tasks()
        self.admin = None
        self.transport = None

    async def run(self) -> None:
        """Connect, subscribe, snapshot state, then drive recv loop with background polling."""
        try:
            async with Admin(ip=self.cfg["ip"], port=self.cfg["port"]) as admin:
                self.admin = admin
                self.transport = AdminTransport(admin)
                await admin.login(self.cfg["admin_name"], self.cfg["admin_pass"])
                for update, freq in (
                    (AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.CONSOLE, AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC),
                    (AdminUpdateType.DATE, AdminUpdateFrequency.MONTHLY),
                ):
                    await admin.subscribe(update, freq)
                self._setup_handlers()
                await self._init_game_state()
                self.running = True
                self.log.info(f"Connected to {self.cfg['ip']}:{self.cfg['port']}")
                await self.msg("Admin connected")
                while self.running:
                    try:
                        async with self._rcon_lock:
                            packets = await asyncio.wait_for(admin.recv(), timeout=0.5)
                    except TimeoutError:
                        packets = []
                    if not packets and self.transport and self.transport.at_eof:
                        raise ConnectionError("Admin connection closed (EOF)")
                    for pkt in packets:
                        await admin.handle_packet(pkt)
        except Exception as e:
            if not isinstance(e, ConnectionError | OSError | TimeoutError):
                self.log.error(f"Bot run error: {e}", exc_info=True)
            raise
        finally:
            await self.cleanup()


async def run_bot(cfg: ServerConfig, log: logging.Logger) -> None:
    """Run bot with automatic reconnect; backoff resets after a stable connection."""
    addr = f"{cfg['ip']}:{cfg['port']}"
    retry_delay = _RECONNECT_DELAY
    while True:
        connected_at = time.monotonic()
        try:
            await Bot(cfg, log).run()
            log.info(f"[{addr}] disconnected")
        except (ConnectionError, OSError, TimeoutError):
            log.warning(f"[{addr}] unavailable")
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
        if time.monotonic() - connected_at >= _RECONNECT_DELAY:
            retry_delay = _RECONNECT_DELAY
        else:
            retry_delay = min(retry_delay * 2, _RECONNECT_MAX_DELAY)
        log.info(f"[{addr}] reconnecting in {retry_delay}s...")
        await asyncio.sleep(retry_delay)


async def main() -> None:
    """Load settings.cfg, configure logging, launch one bot task per server."""
    try:
        servers = load_config("settings.cfg")
    except Exception as e:
        print(f"Error loading settings.cfg: {e}", flush=True)
        return
    if not servers:
        print("No servers configured in settings.cfg", flush=True)
        return
    log_level = logging.DEBUG if any(s.get("debug") for s in servers) else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("[Main]")
    log.info("=== OpenTTD Admin Bot Starting ===")
    valid: list[ServerConfig] = []
    for cfg in servers:
        if errs := normalize_config(cfg, log):
            for error in errs:
                log.error(f"Config error [{cfg.get('name', '?')}]: {error}")
        else:
            valid.append(cast(ServerConfig, cfg))
    if not valid:
        log.error("No valid servers; exiting.")
        return
    log.info(f"Starting {len(valid)} bot instance(s)")
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: shutdown.set())
    tasks = [
        asyncio.create_task(run_bot(c, logging.getLogger(f"[{c['ip']}:{c['port']}]")))
        for c in valid
    ]
    try:
        await shutdown.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
