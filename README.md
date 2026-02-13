# OpenTTD Admin Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![OpenTTD](https://img.shields.io/badge/OpenTTD-15.1+-green.svg)](https://www.openttd.org/)

> Enterprise-grade administrative bot for OpenTTD dedicated servers with multi-server support, automated game management, and player interaction features.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Player Commands](#player-commands)
- [How It Works](#how-it-works)
- [Monitoring & Logging](#monitoring--logging)
- [Troubleshooting](#troubleshooting)
- [Security Considerations](#security-considerations)
- [Performance & Scalability](#performance--scalability)
- [Contributing](#contributing)
- [License](#license)
- [Support](#support)

---

## Overview

The OpenTTD Admin Bot is a robust Python application designed to automate server management tasks for OpenTTD dedicated servers. Built with thread safety and reliability in mind, it handles multiple servers simultaneously while providing essential administrative functions and player engagement features.

### Key Capabilities

- **Automated Server Management**: Pause/unpause, map rotation, company cleanup
- **Multi-Server Support**: Single bot instance manages multiple game servers
- **Player Interaction**: Chat commands, greetings, company value rankings
- **Goal-Based Gameplay**: Automated winner detection and map resets
- **Self-Service Tools**: Player-initiated company resets with confirmation
- **Production Ready**: Thread-safe, error-resilient, 24/7 operation tested

---

## Features

### 🎮 Game Management

#### Auto Pause/Unpause
- **Behavior**: Automatically pauses the game when no companies exist to conserve resources
- **Trigger**: Unpauses immediately when a player creates a company
- **Benefit**: Reduces CPU usage during idle periods on low-population servers

#### Goal Tracking & Map Rotation
- **Monitoring**: Continuously tracks all company values against configured goal
- **Winner Detection**: Announces the winning company when goal threshold is reached
- **Countdown**: 20-second countdown with multiple announcements (20s, 15s, 10s, 5s)
- **Auto-Reload**: Automatically loads the next map from configured save/scenario file
- **State Reset**: Cleans all tracked data for fresh game start

#### Inactive Company Cleanup
- **Criteria**: Removes companies older than `clean_age` years with value below `clean_value`
- **Process**: 
  1. Identifies qualifying companies during periodic checks
  2. Moves all clients to spectator mode
  3. Executes company reset via RCON
  4. Broadcasts notification to all players
- **Configurable**: Adjust age threshold and value threshold independently

### 👥 Player Engagement

#### Welcome Messages
- **Delay**: 5-second delay after join to ensure player is ready
- **Personalization**: Uses player's chosen display name
- **Information**: Includes basic command reference for new players

#### Interactive Commands
Players can interact with the bot using chat commands prefixed with `!`:

| Command | Description | Cooldown |
|---------|-------------|----------|
| `!help` | Displays available command list | 3s |
| `!info` | Shows game goal, gamescript details, and mechanics info | 3s |
| `!rules` | Displays server rules including auto-clean thresholds | 3s |
| `!cv` | Fetches and displays top 10 companies by value | 3s |
| `!reset` | Initiates company reset process with confirmation | 3s |

#### Company Reset Flow
1. Player types `!reset` while in a company
2. Bot requests confirmation and starts 10-second timer
3. Player moves to spectator to confirm
4. Bot resets the company and notifies the player
5. Timer expires if player doesn't move (cancels reset)

### 📊 Hourly Broadcasts
- **Frequency**: Every hour on the hour
- **Content**: Company value rankings (top 10)
- **Purpose**: Keeps players engaged and competitive

### 🔐 Thread Safety
- **Implementation**: All shared state protected by `threading.RLock()`
- **Coverage**: Companies dict, clients dict, game state, pending resets
- **Benefit**: Prevents race conditions in multi-threaded environment

### 🛡️ Error Resilience
- **Strategy**: Try-except blocks in all critical paths
- **Recovery**: Bot continues operation even when individual operations fail
- **Logging**: Comprehensive error logging with stack traces for debugging

---

## Architecture

### System Design

```
┌─────────────────────────────────────────────────────────────┐
│                        Main Process                         │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Thread 1   │  │   Thread 2   │  │   Thread N   │    │
│  │ Server:3977  │  │ Server:3978  │  │ Server:397X  │    │
│  │              │  │              │  │              │    │
│  │  Bot Instance│  │  Bot Instance│  │  Bot Instance│    │
│  │   + RLock    │  │   + RLock    │  │   + RLock    │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                  │                  │             │
└─────────┼──────────────────┼──────────────────┼─────────────┘
          │                  │                  │
          ▼                  ▼                  ▼
    ┌──────────┐       ┌──────────┐       ┌──────────┐
    │ OpenTTD  │       │ OpenTTD  │       │ OpenTTD  │
    │ Server 1 │       │ Server 2 │       │ Server N │
    │ :3977    │       │ :3978    │       │ :397X    │
    └──────────┘       └──────────┘       └──────────┘
```

### Component Overview

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **Main Process** | Configuration loading, thread management, signal handling | Python threading |
| **Bot Instance** | Game state management, player interaction, RCON execution | pyOpenTTDAdmin |
| **Packet Handlers** | Event processing (chat, client join/quit, company events) | Decorator-based handlers |
| **RCON Interface** | Direct command execution with timeout protection | Custom implementation |
| **State Manager** | Thread-safe data structures for game state | RLock-protected dicts |

### Data Flow

```
Player Action → Packet → Handler → State Update → RCON Command → Server Response
                  ↓                      ↓                              ↓
              Logging              Broadcast Msg               Update State
```

---

## Requirements

### Server Requirements
- **OpenTTD Version**: 15.1 or higher (tested on 15.1)
- **Server Type**: Dedicated server with admin port enabled
- **Network**: Admin port accessible from bot host
- **Configuration**: `admin_port` and `admin_password` configured in `openttd.cfg`

### Bot Host Requirements
- **Operating System**: Linux, macOS, or Windows
- **Python**: 3.10 or higher (3.11+ recommended)
- **Memory**: ~50MB per server instance
- **CPU**: Minimal (event-driven, not CPU-intensive)
- **Network**: Stable connection to OpenTTD server(s)

### Python Dependencies
```
pyOpenTTDAdmin>=0.1.0
```

---

## Installation

### Local Development

#### 1. Clone Repository
```bash
git clone https://github.com/nelbinbinag/openttd-admin.git
cd openttd-admin
```

#### 2. Set Up Virtual Environment (Recommended)
```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Linux/macOS:
source .venv/bin/activate

# Windows PowerShell:
.venv\Scripts\Activate.ps1

# Windows Command Prompt:
.venv\Scripts\activate.bat
```

#### 3. Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

#### 4. Configure Settings
```bash
# Copy example configuration
cp settings.example.json settings.json

# Edit with your server details
nano settings.json  # or vim, code, notepad, etc.
```

#### 5. Run Bot
```bash
python main.py
```

---

### Docker Deployment

#### Dockerfile
```dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY main.py .

# Create logs directory
RUN mkdir -p /app/logs

# Run as non-root user
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import sys; sys.exit(0)"

# Start bot
CMD ["python", "-u", "main.py"]
```

#### Option 1: Docker Run
```bash
# Build image
docker build -t openttd-admin-bot .

# Run container
docker run -d \
  --name openttd-bot \
  --restart unless-stopped \
  -v $(pwd)/settings.json:/app/settings.json:ro \
  openttd-admin-bot
```

#### Option 2: Docker Compose
Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  openttd-bot:
    build: .
    container_name: openttd-admin-bot
    restart: unless-stopped
    volumes:
      - ./settings.json:/app/settings.json:ro
      - ./logs:/app/logs
    environment:
      - TZ=UTC
    networks:
      - openttd-network

networks:
  openttd-network:
    external: true
```

Run with:
```bash
docker-compose up -d
```

---

### Production Deployment

#### Systemd Service (Linux)

Create `/etc/systemd/system/openttd-bot.service`:

```ini
[Unit]
Description=OpenTTD Admin Bot
After=network.target

[Service]
Type=simple
User=ottd-bot
Group=ottd-bot
WorkingDirectory=/opt/openttd-bot
Environment="PATH=/opt/openttd-bot/.venv/bin"
ExecStart=/opt/openttd-bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openttd-bot

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/openttd-bot/logs

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable openttd-bot
sudo systemctl start openttd-bot
sudo systemctl status openttd-bot
```

---

## Configuration

### Configuration File: `settings.json`

```json
{
  "server_ip": "127.0.0.1",
  "admin_name": "AdminBot",
  "admin_pass": "your_secure_password_here",
  "admin_ports": [3977],
  "load_map": "yourmap.scn",
  "goal_value": 100000000000,
  "clean_age": 1,
  "clean_value": 1000,
  "debug": false
}
```

### Configuration Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `server_ip` | string | Yes | - | IP address of OpenTTD server |
| `admin_name` | string | Yes | - | Admin username (matches `openttd.cfg`) |
| `admin_pass` | string | Yes | - | Admin password (matches `openttd.cfg`) |
| `admin_ports` | array[int] | Yes | - | List of admin ports to manage |
| `load_map` | string | Yes | - | Map file to load after goal (save/*.sav or scenario/*.scn) |
| `goal_value` | integer | Yes | - | Company value goal to win (in currency units) |
| `clean_age` | integer | Yes | - | Minimum company age in years for auto-cleanup |
| `clean_value` | integer | Yes | - | Maximum company value for auto-cleanup |
| `debug` | boolean | No | false | Enable debug logging |

### Configuration Examples

#### Single Server Setup
```json
{
  "server_ip": "192.168.1.100",
  "admin_name": "admin",
  "admin_pass": "StrongPassword123!",
  "admin_ports": [3977],
  "load_map": "tropical_island.scn",
  "goal_value": 50000000,
  "clean_age": 5,
  "clean_value": 100000,
  "debug": false
}
```

#### Multi-Server Setup
```json
{
  "server_ip": "192.168.1.100",
  "admin_name": "admin",
  "admin_pass": "StrongPassword123!",
  "admin_ports": [3977, 3978, 3979, 3980],
  "load_map": "default_map.sav",
  "goal_value": 100000000,
  "clean_age": 3,
  "clean_value": 50000,
  "debug": false
}
```

### OpenTTD Server Configuration

Ensure your OpenTTD `openttd.cfg` includes:

```ini
[network]
server_admin_port = 3977
admin_password = your_secure_password_here
```

For multiple servers, use different ports (3977, 3978, etc.).

---

## Player Commands

All commands are prefixed with `!` and subject to a 3-second cooldown per player.

### Command Reference

#### `!help`
**Description**: Displays list of available commands  
**Usage**: `!help`  
**Response**: 
```
=== Commands ===
!info !rules !cv !reset
```

#### `!info`
**Description**: Shows game goal, gamescript information, and production mechanics  
**Usage**: `!info`  
**Response**: 
```
=== Game Info ===
Goal: reach 100.0m company value
Gamescript: Production Booster
Primary Industries (Coal, Wood, Oil, Grain, etc)
Transported >70% increases, <50% decreases production
```

#### `!rules`
**Description**: Displays server rules including auto-cleanup thresholds  
**Usage**: `!rules`  
**Response**: 
```
=== Server Rules ===
1. No griefing/sabotage
2. No blocking players
3. No cheating/exploits
4. Be respectful
5. Inactive companies (more than 5years & company value less than 100.0k) will auto-reset
```

#### `!cv`
**Description**: Fetches current company value rankings (top 10)  
**Usage**: `!cv`  
**Response**: 
```
=== Company Value Rankings ===
1. Transport Tycoons Ltd: 45.2m
2. Rail Masters Inc: 32.8m
3. Cargo Express Co: 28.1m
...
```

#### `!reset`
**Description**: Initiates company reset process with confirmation  
**Requirements**: Must be in a company (not spectator)  
**Process**: 
1. Type `!reset`
2. Receive: "Move to spectator within 10s to reset company #1"
3. Move to spectator mode to confirm
4. Company is reset

---

## How It Works

### Core Mechanisms

#### 1. Connection & Authentication
```python
# Bot connects via pyOpenTTDAdmin
admin = Admin(ip=server_ip, port=admin_port)
admin.login(admin_name, admin_pass)

# Subscribes to events
admin.subscribe(AdminUpdateType.CHAT, AdminUpdateFrequency.AUTOMATIC)
admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
```

#### 2. Event Handling
The bot uses decorator-based packet handlers:

```python
@admin.add_handler(openttdpacket.ChatPacket)
def on_chat(admin, pkt):
    # Process chat messages and commands
    pass

@admin.add_handler(openttdpacket.ClientJoinPacket)
def on_client_join(admin, pkt):
    # Greet new players
    pass
```

#### 3. RCON Data Fetching
Instead of relying solely on packets, the bot uses RCON for reliability:

```python
companies_output = rcon("companies")
clients_output = rcon("clients")
date_output = rcon("get_date")

# Parse with regex
for match in COMPANY_RE.finditer(companies_output):
    cid, name, year, money, loan, value = match.groups()
    # Store in thread-safe dict
```

#### 4. Periodic Tasks
60-second tick for maintenance tasks:
- Poll RCON for current state
- Check for inactive companies
- Check goal achievement
- Auto-pause if no companies

#### 5. Thread Safety Pattern
```python
# All shared state access wrapped
with self._lock:
    companies_to_clean = []
    for cid, data in self.companies.items():
        # Safe read
        pass

# RCON outside lock to avoid deadlock
for cid in companies_to_clean:
    self.rcon(f"reset_company {cid}")
```

---

## Monitoring & Logging

### Log Levels

| Level | Usage | Example |
|-------|-------|---------|
| `DEBUG` | RCON commands, packet details | `RCON> companies` |
| `INFO` | Normal operations, player actions | `Greet: Player1 (#1)` |
| `WARNING` | Recoverable issues | `RCON timeout for command` |
| `ERROR` | Operation failures | `Error in auto_clean: ...` |

### Enabling Debug Logging

Set in `settings.json`:
```json
{
  "debug": true
}
```

### Log Output Examples

#### Normal Operation
```
2024-02-13 10:30:45 INFO Bot === OpenTTD Admin Bot Starting ===
2024-02-13 10:30:45 INFO [Server:3977] Connected to 192.168.1.100:3977
2024-02-13 10:30:50 INFO [Server:3977] Greet: Player1 (#1)
2024-02-13 10:31:30 INFO [Server:3977] Cmd: !cv from #1
2024-02-13 10:45:00 INFO [Server:3977] Hourly CV broadcast
```

#### Debug Mode
```
2024-02-13 10:30:45 DEBUG [Server:3977] RCON> companies
2024-02-13 10:30:45 DEBUG [Server:3977] RCON< #1(Player) Company Name: 'Transport Co'...
2024-02-13 10:30:45 DEBUG [Server:3977] Poll: 3 cos, 5 cls, year=1950
2024-02-13 10:30:46 DEBUG [Server:3977] Chat received: cid=1 msg='!cv'
```

### Monitoring Bot Health

#### Check Process Status
```bash
# Systemd
sudo systemctl status openttd-bot

# Docker
docker ps | grep openttd-bot
```

#### View Live Logs
```bash
# Systemd
sudo journalctl -u openttd-bot -f

# Docker
docker logs -f openttd-bot
```

---

## Troubleshooting

### Common Issues

#### Bot Won't Start

**Solutions**:
```bash
# Verify settings.json exists and is valid
cat settings.json | python -m json.tool

# Check Python version
python --version  # Should be 3.10+

# Reinstall dependencies
pip install --force-reinstall -r requirements.txt
```

#### Cannot Connect to Server

**Solutions**:
```bash
# Test network connectivity
ping <server_ip>
telnet <server_ip> <admin_port>

# Verify OpenTTD server config
grep -A5 "\[network\]" openttd.cfg
```

#### Commands Not Working

**Checklist**:
- [ ] Commands start with `!` (e.g., `!help` not `help`)
- [ ] Wait 3 seconds between commands (cooldown)
- [ ] Bot shows as connected in logs
- [ ] Game is not paused

---

## Security Considerations

### Admin Password

⚠️ **CRITICAL**: The admin password grants **complete server control**

**Best Practices**:
1. **Strong Password**: Use 20+ character random password
   ```bash
   # Generate secure password
   openssl rand -base64 32
   ```

2. **Restrict Access**: 
   - Limit file permissions: `chmod 600 settings.json`
   - Firewall admin ports from public internet

### File Permissions

```bash
# Settings should not be world-readable
chmod 600 /opt/openttd-bot/settings.json
```

---

## Performance & Scalability

### Resource Requirements

| Servers | CPU | Memory | Network |
|---------|-----|--------|---------|
| 1 | <1% | ~50MB | <1 Mbps |
| 5 | <5% | ~250MB | <5 Mbps |
| 10 | <10% | ~500MB | <10 Mbps |

*Measured on Intel i5 processor*

---

## Contributing

We welcome contributions! 

### Development Setup

```bash
# Fork and clone
git clone https://github.com/your-username/openttd-admin.git
cd openttd-admin

# Create feature branch
git checkout -b feature/your-feature-name

# Set up development environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Make changes and test
python main.py
```

### Contribution Guidelines

1. **Code Style**: Follow existing style (PEP 8)
2. **Thread Safety**: Always use `with self._lock:` for shared state
3. **Error Handling**: Wrap risky operations in try-except
4. **Testing**: Test with real OpenTTD servers
5. **Documentation**: Update README for new features

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Support

### Getting Help

- **Documentation**: This README
- **Issues**: [GitHub Issues](../../issues)
- **Discussions**: [GitHub Discussions](../../discussions)

### Maintainers

- [@nelbinbinag](https://github.com/nelbinbinag)

### Acknowledgments

- [OpenTTD Team](https://www.openttd.org/) for the amazing game
- [pyOpenTTDAdmin](https://github.com/ropenttd/pyopenttdadmin) library maintainers
- All contributors and users

---

**Made with ❤️ for the OpenTTD community**

*Last updated: 2024-02-13*
