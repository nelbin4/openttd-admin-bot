# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Modern, resilient admin-port bot for **OpenTTD 15.x+**. Auto-pauses idle maps, tracks company value race-to-goal, cleans inactive companies, lets players self-reset, and reloads the scenario when someone wins.

## What’s inside

- **Single-server bot:** `main.py`
- **Multi-server bot (up to 10 servers):** `main10.py` — requires `settings.json`
- **Python-only stack:** built on [`pyOpenTTDAdmin`](https://pypi.org/project/pyOpenTTDAdmin/)

## Highlights

- **Auto pause/unpause** when the map is empty; instant unpause on first active company.
- **Goal tracking**: watches top company value vs. configured goal; faster polling at 90%/95%; announces winner → countdown → loads new scenario.
- **Dead company cleanup**: resets aged/low-value companies and moves players to spectators first.
- **Player self-service**: `!reset` + `!yes` with safety checks for company changes/quit.
- **Startup hygiene**: removes default “Unnamed” company; greets joiners.
- **RCON safety**: serialized calls, retries, circuit breaker, buffered responses.
- **Caching & speed**: TTL-based company/client caches with periodic refresh and backoff.
- **Resilience**: reconnect attempts, thread pool workers, memory/log visibility.

## Files & entrypoints

| File | Purpose |
| --- | --- |
| `main.py` | Single-server bot (configure inline or via small settings file). |
| `main10.py` | Multi-server runner (reads `settings.json`; supports up to 10 servers). |
| `settings.json` | Multi-server config: admin/game ports, credentials, scenario, thresholds. |
| `requirements.txt` | Python dependencies (primary: `pyOpenTTDAdmin`). |

## Requirements

- OpenTTD ≥ 15.x with **admin port open** (TCP 3977 default)
- Python **3.10 – 3.13**
- Access to your scenario file on the server (e.g., `content_download/scenario`)

## Configuration

### Single server (main.py)
Set values inside `main.py` or supply a minimal `settings.json` with one port pair. At minimum:

```json
{
  "server_ip": "127.0.0.1",
  "admin_name": "Admin",
  "admin_pass": "CHANGE_ME",
  "admin_ports": [3977],
  "game_ports": [3979],
  "load_scenario": "your_map.scn",
  "goal_value": 10000000000,
  "dead_co_age": 5,
  "dead_co_value": 5000000,
  "rcon_retry_max": 3,
  "rcon_retry_delay": 0.5,
  "reconnect_max_attempts": 10,
  "reconnect_delay": 5.0,
  "reset_countdown_seconds": 20
}
```

### Multi-server (main10.py)
`main10.py` reads **settings.json** with arrays of ports. Up to **10 servers** are started—one per `admin_port` entry.

Required keys in `settings.json`:

- `server_ip`
- `admin_name`, `admin_pass`
- `admin_ports` (list, up to 10)
- `game_ports` (list, same length as `admin_ports`)
- `load_scenario`, `goal_value`
- `dead_co_age`, `dead_co_value`
- `rcon_retry_max`, `rcon_retry_delay`
- `reconnect_max_attempts`, `reconnect_delay`
- `reset_countdown_seconds`

See the provided `settings.json` for a working multi-server example.

## Chat commands

Command | Action
---|---
`!help` | List commands
`!info` | Server description + goal amount
`!rules` | Basic server rules
`!cv` | Top 10 company values + % to goal
`!reset` | Request to delete your current company
`!yes` | Confirm deletion (30 s timeout)

## Run locally

```bash
git clone https://github.com/nelbinbinag/openttd-admin
cd openttd-admin

python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate          # Windows

pip install -r requirements.txt

# Single server
python main.py

# Multi-server (requires settings.json)
python main10.py
```

## Docker (production-friendly)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main10.py"]
```

```bash
docker build -t openttd-admin-bot .
docker run -d --restart unless-stopped \
  --name openttdbot \
  -v $(pwd)/settings.json:/app/settings.json \
  -v $(pwd)/logs:/app/logs \
  openttd-admin-bot
```

For single server inside Docker, change the CMD to `main.py`.

## Operational notes

- `load_scenario` must exist on the OpenTTD host.
- Use a **strong admin password**—possession grants full control.
- The bot trims unnamed companies and pauses idle maps to save CPU.
- Goal handling: winner announcement → reset countdown → scenario reload → state reset.

## License

MIT

---
PRs and issues welcome.
