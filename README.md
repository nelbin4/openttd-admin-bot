# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenTTD](https://img.shields.io/badge/OpenTTD-14.0+-green.svg)](https://www.openttd.org/)

Async Python bot for managing OpenTTD multiplayer servers with auto-pause, goal tracking, company cleanup, and player engagement features. Built with `aiopyopenttdadmin` for efficient multi-server management.

## Features
- Auto pause/unpause when no companies exist or when players join
- Goal tracking with winner announcements and automatic map reloads
- Auto cleanup of old, low-value companies based on configurable thresholds
- Player engagement with welcome messages, chat commands, and hourly rankings
- Self-service company reset via `!reset` with spectator confirmation
- Anti-griefing limit: enforces `MAX_COMPANIES_PER_IP` per client and resets extras
- Multi-server support from a single async instance
- Event-driven asyncio architecture with minimal resource usage
- Error resilience with auto-reconnect and graceful shutdown

## Requirements
- OpenTTD 14.0+ dedicated server with admin port enabled
- Python 3.10 or higher
- Dependencies: `pyopenttdadmin` (includes `aiopyopenttdadmin`)

## Quick Start
```bash
git clone https://github.com/yourusername/openttd-admin-bot.git
cd openttd-admin-bot
python -m venv venv
# On Linux/macOS
source venv/bin/activate
# On Windows
venv\Scripts\activate
pip install pyopenttdadmin
cp settings.example.cfg settings.cfg
python main.py
```

## Docker
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir pyopenttdadmin
COPY main.py settings.cfg ./
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser
CMD ["python", "-u", "main.py"]
```
```bash
docker build -t openttd-bot .
docker run -d --name openttd-bot --restart unless-stopped \
  -v $(pwd)/settings.cfg:/app/settings.cfg:ro openttd-bot
```

## Configuration
`settings.cfg`
```ini
[server1]
ip = 127.0.0.1
port = 3977
admin_name = Admin
admin_pass = password
map = competitive.sav
goal = 100000000
clean_age = 5
clean_value = 100000
debug = false

[server2]
ip = 127.0.0.1
port = 3978
admin_name = Admin
admin_pass = password
map = casual.scn
goal = 50000000
clean_age = 3
clean_value = 50000
debug = false
```
Add more servers by adding `[server3]`, `[server4]`, and so on.

### Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| `ip` | string | OpenTTD server IP address |
| `port` | int | Admin port for this server |
| `admin_name` | string | Admin username (matches openttd.cfg) |
| `admin_pass` | string | Admin password (matches openttd.cfg) |
| `map` | string | Map file after goal: `map.sav` or `scenario.scn` |
| `goal` | int | Company value goal to win |
| `clean_age` | int | Minimum company age (years) for auto-cleanup |
| `clean_value` | int | Maximum company value for auto-cleanup |
| `debug` | bool | Enable debug logging |

## Player Commands (3-second cooldown, ignored when paused)
| Command | Description |
|---------|-------------|
| `!help` | Show available commands |
| `!info` | Display game goal and mechanics |
| `!rules` | Show server rules and cleanup thresholds |
| `!cv` | Company value rankings (top 10) |
| `!reset` | Reset your company (requires spectator confirmation within 15s) |

## How It Works
- Auto-clean: companies reset if age >= `clean_age` and value < `clean_value` (checked every 60s)
- Goal system: when company value >= `goal`, announces winner, counts down, reloads map, and resets state
- Pause detection: tracks date changes; paused games ignore commands and greet accordingly

## Troubleshooting
- Enable debug: set `debug = true` in settings.cfg
- Connection issues: ensure `server_admin_port` and `admin_password` are set in openttd.cfg; test with `telnet 127.0.0.1 3977`
- Map loading: verify file exists in OpenTTD `save/` or `scenario/`; use relative paths like `map.sav`
- Commands ignored: commands are blocked while paused and limited by the 3-second cooldown

## Security Best Practices
```bash
openssl rand -base64 32 > admin_pass.txt
chmod 600 settings.cfg
# Run as non-root
useradd -r -s /bin/false ottdbot
chown ottdbot:ottdbot main.py settings.cfg
sudo -u ottdbot python main.py
```

## License
MIT License - see [LICENSE](LICENSE)

## Contributing
Contributions are welcome. Open issues for bugs or feature requests.
