# OpenTTD Admin Bot

Lightweight admin-port bot for **OpenTTD 15.x+** dedicated servers.  
Keeps the game paused when empty, tracks company value race to goal, auto-cleans inactive companies, lets players self-reset, auto-restarts map after winner.

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Features

- **Auto pause/unpause**  
  Pauses when no active (non-spectator) companies exist. Unpauses instantly when first player joins/creates company.

- **Startup & cleanup**  
  Removes default "Unnamed" company on connect and periodically.  
  Greets every joining player with welcome + command hint.

- **Goal race**  
  Monitors top company value vs configured goal (default 10 billion).  
  Shows progress % with `!cv`.  
  Faster polling at 90% and 95% of goal.  
  Announces winner → 20-second countdown → loads new scenario.

- **Dead company auto-reset**  
  Companies founded > `DEAD_CO_AGE` years ago with value < `DEAD_CO_VALUE` → auto reset.  
  Players moved to spectators first.

- **Player self-reset**  
  `!reset` → 30-second confirmation window → `!yes` deletes company.  
  Safe: checks current company, cancels on switch/quit.

- **RCON safety**  
  Serialized RCON commands, timeout handling, response buffering, cache TTL (default 5 s).

- **Threading & resilience**  
  Thread pool for commands & background tasks.  
  Locks for shared state. Graceful reconnect attempts.

## Chat Commands

Command     | Action
------------|-----------------------------------------------
`!help`     | List commands
`!info`     | Server description + goal amount
`!rules`    | Basic server rules
`!cv`       | Top 10 company values + % to goal (fresh RCON)
`!reset`    | Request to delete your current company
`!yes`      | Confirm deletion (must be typed within 30 s)

## Requirements

- OpenTTD ≥ 15.x with **admin port open** (TCP 3977 default)
- Python **3.10** – **3.12** recommended
- `pyOpenTTDAdmin` (only external dependency)

```bash
pip install pyOpenTTDAdmin
```

## Quick Start (local)

```bash
git clone https://github.com/nelbinbinag/openttdbot.git
cd openttdbot

python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install pyOpenTTDAdmin

# Copy & edit config
cp main2.py main.py
# Edit main.py → at minimum change:
#   SERVER_IP, ADMIN_PASS, LOAD_SCENARIO

python main.py
```

## Configuration (in `main.py`)

Critical fields:

```python
class Config:
    SERVER_IP     = "127.0.0.1"              # your server IP
    SERVER_PORT   = 3977
    ADMIN_NAME    = "Admin"
    ADMIN_PASS    = "strongsecret123"        # CHANGE THIS
    GOAL_VALUE    = 10_000_000_000           # win condition
    LOAD_SCENARIO = "my_scenario.scn"        # filename only
    DEAD_CO_AGE   = 5                        # years inactive
    DEAD_CO_VALUE = 5_000_000                # min value threshold
    DEBUG         = True                     # detailed logs
```

Other common tunables:

- `CACHE_TTL`            → how long cache is valid (s)
- `RCON_TIMEOUT`         → max wait per rcon command (s)
- `MSG_RATE_LIMIT`       → seconds between chat messages
- `MONITOR_INTERVAL_*`   → polling frequency (default / 90% / 95%)
- `PAUSE_DELAY`          → seconds before auto-pause
- `RESET_TIMEOUT`        → confirmation window (s)

## Docker (recommended for production)

`Dockerfile`

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python", "main.py"]
```

`requirements.txt`

```
pyOpenTTDAdmin
```

```bash
docker build -t openttd-admin-bot .
docker run -d --restart unless-stopped \
  --name openttdbot \
  -v $(pwd)/config:/app \
  -v $(pwd)/logs:/app/logs \
  openttd-admin-bot
```

Best: mount your edited `main.py` into `/app/main.py`.

## Important

- Bot **cannot** read `openttd.cfg` → hardcode correct IP/port/pass.
- `LOAD_SCENARIO` must exist in OpenTTD’s **content_download/scenario** folder.
- Strong admin password required — anyone with it owns the server.
- `DEBUG=True` produces lots of output — turn off in production.

## License

MIT

PRs, issues → https://github.com/nelbinbinag/openttdbot/
