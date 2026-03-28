# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenTTD](https://img.shields.io/badge/OpenTTD-14.0+-green.svg)](https://www.openttd.org/)

Async Python bot for managing OpenTTD multiplayer servers. Handles auto-pause, goal tracking, company cleanup, and player engagement. Supports multiple servers from a single instance.

## Features
- Auto pause/unpause based on company presence
- Goal tracking with winner announcement and automatic map reload
- Auto-clean old low-value companies
- Company limit enforcement: max `max_companies` companies per client
- Welcome messages with country detection, chat commands, leaderboard broadcasts
- Self-service `!reset` with spectator confirmation timeout
- Multi-server support via single async process
- Auto-reconnect on connection loss

## Requirements
- OpenTTD 14.0+ dedicated server with admin port enabled
- Python 3.10+
- `pip install aiopyopenttdadmin`

## Quick Start
```bash
git clone https://github.com/nelbin4/openttd-admin-bot.git
cd openttd-admin-bot
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install aiopyopenttdadmin
# Edit settings.cfg then:
python main.py
```

## Docker
```bash
docker build -t openttd-bot .
docker run -d --name openttd-bot --restart unless-stopped \
  -v $(pwd)/settings.cfg:/app/settings.cfg:ro openttd-bot
```
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir aiopyopenttdadmin
COPY main.py settings.cfg requirements.txt ./
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser
CMD ["python", "-u", "main.py"]
```

## Configuration — `settings.cfg`
```ini
[server1]
ip = 127.0.0.1
port = 3977
admin_name = admin
admin_pass = password
map = somemap.scn
goal = 500000000
clean_age = 5
clean_value = 5000000
max_companies = 2
broadcast_cv = 3600
debug = false

# Add more servers as [server2], [server3], etc.
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `ip` | string | Server IP address |
| `port` | int | Admin port |
| `admin_name` | string | Admin username (matches openttd.cfg) |
| `admin_pass` | string | Admin password (matches openttd.cfg) |
| `goal` | int | Company value win condition (0 = disabled) |
| `map` | string | Map to load after goal: `.sav`, `.scn`, or `newgame` |
| `clean_age` | int | Minimum company age in years before auto-clean eligibility |
| `clean_value` | int | Companies below this value are eligible for auto-clean |
| `max_companies` | int | Max companies allowed per client |
| `broadcast_cv` | int | Leaderboard broadcast interval in seconds |
| `debug` | bool | Enable debug logging |

## Chat Commands
| Command | Description |
|---------|-------------|
| `!info` | Game goal and mechanics |
| `!rules` | Server rules and auto-clean thresholds |
| `!cv` | Company value leaderboard |
| `!reset` | Reset your company — move to spectator within 15s to confirm |

Commands are blocked while the game is paused and rate-limited per client (2s cooldown).

## How It Works
- **Pause**: game pauses when no companies exist, unpauses when the first company is created
- **60s poll**: `rcon companies` runs at every wall-clock `:00` second; updates value cache, triggers goal check and auto-clean
- **Auto-clean**: resets companies where age ≥ `clean_age` AND value < `clean_value`; moves clients to spectator first
- **Goal**: when any company reaches `goal`, announces winner, counts down 20s, reloads map
- **Limit enforcement**: if a client already owns `max_companies` companies, any extra company is immediately removed
- **New game**: default company #1 is always reset if unoccupied on map load

## Troubleshooting
- **Connection refused**: confirm `server_admin_port` and `admin_password` in `openttd.cfg`
- **Commands ignored**: blocked while paused or within 2s cooldown window
- **Map not loading**: verify file exists in OpenTTD `save/` or `scenario/`; use filename only (e.g. `map.scn`)
- **Debug logs**: set `debug = true` in settings.cfg

## Security
```bash
chmod 600 settings.cfg
useradd -r -s /bin/false ottdbot
sudo -u ottdbot python main.py
```
