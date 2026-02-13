# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> A lightweight python based app for Openttd Admin Bot, managing single or multiple servers.

## Highlights

- **Auto pause/unpause** pauses when no company; unpause when there is one.
- **Goal tracking**: watches company value, announces winner → loads new map.
- **Dead company cleanup**: automatically resets aged/low-value companies.
- **Greet message**: greet newly connected clients.
- **Reset self-service**: clients can chat `!reset` and moving to spectator for company reset.
- **Rcon data**: uses rcon commands on companies, clients, and date for reliability instead of packets.

## Files & entrypoints

| File                | Purpose                                                                   |
| ----------------    | -------------------------------------                                     |
| `main.py`           | Main entry point; supports many servers. |
| `settings.json`     | Settings file; set ip, password, etc. |
| `requirements.txt`  | Python dependencies (`pyOpenTTDAdmin`).                          |

## Requirements

- Dedicated OpenTTD server with admin port opened
- Python 3.10+
- Upload a map to load when goal reached (folder sav `save` or scn `scenario`)

## Configuration

Set values inside `settings.json`. Multiple admin ports -- higher server count depend on hardware/network headroom.

```json
{
  "server_ip": "127.0.0.1",    // openttd dedicated server ip
  "admin_name": "admin",       // admin name
  "admin_pass": "password",    // admin password
  "admin_ports": [3977],       // add for multi-server [3976, 3977, 3978, 3979]
  "load_map": "yourmap.scn",   // load map after goal reached /save folder for .sav or /scenario folder for .scn
  "goal_value": 100000000000,  // company value goal
  "clean_age": 1,              // auto clean grace period in yrs
  "clean_value": 1000,         // auto clean company value threshold
  "debug": false               // for console logger
}
```

## Chat commands

Command | Action
---|---
`!help`    | List commands
`!info`    | Goal and Gamescript information
`!rules`   | Server rules
`!cv`      | Ranking company values
`!reset`   | Request reset your current company

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
COPY main.py .
CMD ["python", "main.py"]
```

## Operational notes

- tested with OpenTTD 15.1
- Use a **strong admin password**—possession grants full control.

## License

MIT

---
PRs and issues welcome.
