# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenTTD](https://img.shields.io/badge/OpenTTD-14.0+-green.svg)](https://www.openttd.org/)

Async Python bot for managing OpenTTD multiplayer servers with auto-pause, goal tracking, company cleanup, and player engagement features. Built with `aiopyopenttdadmin` for efficient multi-server management.

## ✨ Features

- **🎮 Auto Pause/Unpause** - Automatically pauses when no companies exist, unpauses when players join
- **🏆 Goal Tracking** - Monitors company values, announces winners, auto-reloads maps on goal completion
- **🧹 Auto Cleanup** - Resets old/low-value companies automatically based on configurable thresholds
- **💬 Player Engagement** - Welcome messages with pause detection, chat commands, hourly CV rankings
- **🔄 Self-Service Reset** - Players can reset their companies via `!reset` with spectator confirmation
- **🔧 Multi-Server** - Single bot instance manages 1-15+ servers concurrently with async architecture
- **⚡ Async Architecture** - Efficient event-driven design using asyncio, minimal resource usage
- **🛡️ Error Resilient** - Auto-reconnect, graceful shutdown, comprehensive error handling

## 📋 Requirements

- **OpenTTD**: 14.0+ dedicated server with admin port enabled
- **Python**: 3.10 or higher
- **Dependencies**: `pyopenttdadmin` (includes `aiopyopenttdadmin`)

## 🚀 Quick Start

### Installation

```bash
# Clone repository
git clone https://github.com/yourusername/openttd-admin-bot.git
cd openttd-admin-bot

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Install dependencies
pip install pyopenttdadmin

# Configure settings
cp settings.example.cfg settings.cfg
nano settings.cfg  # Edit with your server details

# Run bot
python main.py
```

### Docker Deployment

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

## ⚙️ Configuration

### settings.cfg

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

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `ip` | string | OpenTTD server IP address |
| `port` | int | Admin port for this server |
| `admin_name` | string | Admin username (matches openttd.cfg) |
| `admin_pass` | string | Admin password (matches openttd.cfg) |
| `map` | string | Map file after goal: `map.sav` or `scenario.scn` |
| `goal` | int | Company value goal to win (e.g., 100000000) |
| `clean_age` | int | Minimum company age (years) for auto-cleanup |
| `clean_value` | int | Maximum company value for auto-cleanup |
| `debug` | bool | Enable debug logging (default: false) |

**Note**: Add more servers by creating additional `[server3]`, `[server4]` sections.

## 💡 How It Works

### Player Commands (3-second cooldown, ignored when paused)

| Command | Description |
|---------|-------------|
| `!help` | Show available commands |
| `!info` | Display game goal and mechanics |
| `!rules` | Show server rules and cleanup thresholds |
| `!cv` | Company value rankings (top 10) |
| `!reset` | Reset your company (requires spectator confirmation within 15s) |

### Key Features

**Auto-Clean**: Companies reset if age ≥ `clean_age` years AND value < `clean_value` (checked every 60s)

**Goal System**: When value ≥ `goal`, announces winner → countdown (20s/15s/10s/5s) → reloads map → fresh game

**Pause Detection**: Uses date change tracking for accuracy. Paused = date stale → commands ignored, greeting mentions pause

## 🔍 Troubleshooting

**Enable Debug**: Set `debug = true` in settings.cfg

**Connection Issues**: Verify OpenTTD `openttd.cfg` has `server_admin_port` and `admin_password` configured. Test: `telnet 127.0.0.1 3977`

**Map Loading**: Verify file exists in OpenTTD's `save/` or `scenario/` directory. Use relative path like `map.sav`. Test: `rcon_pw <pass> "load map.sav"`

**Commands Not Working**: Commands are ignored when game is paused (no companies). Check 3-second cooldown.

## 🏗️ Technical Details

**Async Architecture**: Event-driven design with asyncio, single-threaded event loop handles 15+ servers efficiently (~15MB total vs ~360MB with threading).

**Dependencies**: `pyopenttdadmin>=1.0.0` (includes both sync and async modules)

## 🔒 Security Best Practices

```bash
# Generate strong admin password
openssl rand -base64 32

# Protect configuration file
chmod 600 settings.cfg

# Run as non-root user
useradd -r -s /bin/false ottdbot
chown ottdbot:ottdbot main.py settings.cfg
sudo -u ottdbot python main.py
```

## 📄 License

MIT License - see [LICENSE](LICENSE) file

## 🙏 Acknowledgments

- [OpenTTD Team](https://www.openttd.org/) - Amazing game and admin protocol
- [pyOpenTTDAdmin](https://github.com/ropenttd/pyopenttdadmin) - Python library for OpenTTD admin protocol

## 🤝 Contributing

Contributions welcome! Please open issues for bugs or feature requests.

---

**Made with ❤️ for the OpenTTD community**
