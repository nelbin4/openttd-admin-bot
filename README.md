# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenTTD](https://img.shields.io/badge/OpenTTD-15.1+-green.svg)](https://www.openttd.org/)

## Features

- **Auto Pause/Unpause** - Pauses when no companies exist; unpauses when players join
- **Goal Tracking** - Monitors company values, announces winners, auto-loads next map
- **Company Cleanup** - Automatically resets inactive/low-value companies
- **Player Engagement** - Welcome messages, chat commands, hourly rankings
- **Self-Service Reset** - Players can reset their own companies with `!reset`
- **Multi-Server** - Single bot instance manages multiple servers
- **Thread-Safe** - RLock protection prevents race conditions
- **Error Resilient** - Continues operation despite individual failures

## Requirements

- **OpenTTD**: 15.1+ dedicated server with admin port enabled
- **Python**: 3.10 or higher

## Quick Start

### Installation

```bash
# Clone repository
git clone https://github.com/nelbinbinag/openttd-admin.git
cd openttd-admin

# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Configure settings
cp settings.example.json settings.json
nano settings.json  # Edit with your server details

# Run bot
python main.py
```

### Docker Deployment

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser
CMD ["python", "-u", "main.py"]
```

```bash
docker build -t openttd-bot .
docker run -d --name openttd-bot --restart unless-stopped \
  -v $(pwd)/settings.json:/app/settings.json:ro openttd-bot
```

## Configuration

### settings.json

```json
{
  "server_ip": "127.0.0.1",
  "admin_name": "Admin",
  "admin_pass": "password",
  "admin_ports": [3977],
  "load_map": "yourmap.scn",
  "goal_value": 100000000,
  "clean_age": 5,
  "clean_value": 100000,
  "debug": false
}
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `server_ip` | string | OpenTTD server IP address |
| `admin_name` | string | Admin username (matches openttd.cfg) |
| `admin_pass` | string | Admin password (matches openttd.cfg) |
| `admin_ports` | array[int] | List of admin ports to manage [e.g. 3967,3968,3969,3970] |
| `load_map` | string | Map file after goal: save/*.sav or scenario/*.scn |
| `goal_value` | int | Company value goal to win |
| `clean_age` | int | Min company age (years) for auto-cleanup |
| `clean_value` | int | Max company value for auto-cleanup |
| `debug` | bool | Enable debug logging (default: false) |

## How It Works

### Player Commands

All commands use `!` prefix with 3-second cooldown:

| Command | Description |
|---------|-------------|
| `!help` | Show available commands |
| `!info` | Display game goal and mechanics |
| `!rules` | Show server rules and cleanup thresholds |
| `!cv` | Company value rankings (top 10) |
| `!reset` | Reset own company (requires spectator confirmation) |

### Reset Process
1. Type `!reset` while in a company
2. Bot responds: "Move to spectator within 10s to reset company #X"
3. Move to spectator to confirm
4. Company is reset

### Auto-Clean Logic
Companies are reset if:
- Age >= `clean_age` years **AND** company value < `clean_value`
- Clients joined to company will be moved to spectators
- This is checked every 60s (loop)

### Goal System
1. Monitors all company values every 60 seconds
2. When value >= `goal_value`: announces winner
3. 20-second countdown (20s, 15s, 10s, 5s)
4. Loads map from `load_map` configuration
5. Resets all state for fresh game

## Troubleshooting

### Enable Debug Mode
Modify value in settings.json
```json
{ "debug": true }
```

### Bot Won't Connect
```bash
# try adding this on openttd.cfg
[version]
version_string = 15.1
version_number = 1F086D64
ini_version = 7

```

### Map Won't Load After Goal
- Verify file exists in `/save/` or `/scenario/` folder
- Filename in settings.json must match exactly (case-sensitive)
- Test manually: `rcon_pw <pass> "load yourmap.sav"`

**Best Practices**:
```bash
# Generate strong password
openssl rand -base64 32

# Protect settings file
chmod 600 settings.json
```

### File Permissions
```bash
# Settings should not be world-readable
chmod 600 /opt/openttd-bot/settings.json

# Program can be read-only
chmod 444 /opt/openttd-bot/main.py
```

## License

MIT License - see [LICENSE](LICENSE) file

## Acknowledgments

- [OpenTTD Team](https://www.openttd.org/)
- [pyOpenTTDAdmin](https://github.com/ropenttd/pyopenttdadmin) library

---

**Made for the OpenTTD community**
