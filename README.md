# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Modern, resilient admin-port bot for **OpenTTD 15.x+**. Auto-pauses idle maps, tracks company value race-to-goal, cleans inactive companies, lets players self-reset, and reloads the scenario when someone wins.

## What’s inside

- **Server bot (1 to many servers):** `main.py` — requires `settings.json`; practical limit depends on hardware/network capacity
- **Python-only stack:** built on [`pyOpenTTDAdmin`](https://pypi.org/project/pyOpenTTDAdmin/)

## Highlights

- **Auto pause/unpause** when the map is empty; instant unpause on first active company.
- **Goal tracking**: watches top company value, announces winner → countdown → loads new scenario.
- **Dead company cleanup**: resets aged/low-value companies.
- **Player self-service**: `!reset` + `!yes` for company reset request.
- **Startup hygiene**: removes default “Unnamed” company; greets joiners.
- **RCON safety**: serialized calls, retries, circuit breaker, buffered responses.
- **Caching & speed**: TTL-based company/client caches with periodic refresh and backoff.
- **Resilience**: reconnect attempts, thread pool workers, memory/log visibility.

## Files & entrypoints

| File                | Purpose                                                                   |
| ----------------    | -------------------------------------                                     |
| `main.py`           | Main entry point (reads `settings.json`; supports many servers; practical limit depends on hardware). |
| `settings.json`     | Multi-server config: admin ports (optional game_ports must match length), credentials, scenario, thresholds. |
| `requirements.txt`  | Python dependencies (primary: `pyOpenTTDAdmin`).                          |

## Requirements

- OpenTTD ≥ 15.x with **admin port open** (TCP 3977 default)
- Python **3.10 – 3.13**
- Access to your scenario file on the server (e.g., `content_download/scenario`)

## Configuration

### Configuration (main.py)
Set values inside `settings.json`. `main.py` reads arrays of ports and will start one bot per `admin_port` entry. Typical setups run 1–10 servers; higher counts depend on your hardware/network headroom.

```json
{
  "server_ip": "127.0.0.1",         // openttd ip
  "admin_name": "Admin",
  "admin_pass": "password",         // change password
  "admin_ports": [3977],            // add more for multi-server, e.g., [3977, 3978, 3979]
  "load_scenario": "your_map.scn",  // load a scenario map
  "goal_value": 10000000000,
  "dead_co_age": 5,                 // years before auto-clean policy
  "dead_co_value": 5000000,         // autoclean threshhold company value
  "rcon_retry_max": 3,
  "rcon_retry_delay": 0.5,
  "reconnect_max_attempts": 10,
  "reconnect_delay": 5.0,
  "reset_countdown_seconds": 20       // seconds to load scenario map when goal achieved
}
```

## Chat commands

Command | Action
---|---
`!help`    | List commands
`!info`    | Server description + goal amount
`!rules`   | Basic server rules
`!cv`      | Top 10 company values + % to goal
`!reset`   | Request to delete your current company
`!yes`     | Confirm deletion (30s timeout), used when !reset

## Run locally

```bash
git clone https://github.com/nelbinbinag/openttd-admin
cd openttd-admin

python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate          # Windows

pip install -r requirements.txt

python main.py
```

> Note: `main.py` adjusts `sys.path` so the bundled `pyOpenTTDAdmin` is importable without a virtualenv. A venv is still recommended for dependency isolation, but not required for imports.

## Docker (production-friendly)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
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
- At startup, bot trims "Unnamed" company and pauses idle map. Auto unpause when company is created.
- "Unnamed" company is generated when scenario map is used on a multiplayer game.
- Goal handling: winner announcement → start reset countdown (default: 20s) → scenario load → state reset.

## License

MIT

---
PRs and issues welcome.
