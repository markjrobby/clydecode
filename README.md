# Telegram Claude Bot
<img width="1024" height="1024" alt="Gemini_Generated_Image_vsats2vsats2vsat" src="https://github.com/user-attachments/assets/0ee79bdf-53bf-428c-9aa4-35d78b59d83c" />


A Telegram bot that bridges to Claude Code via the Claude Agent SDK. Run Claude Code from anywhere using Telegram.

Inspired by [droid-telegram-bot](https://github.com/factory-ben/droid-telegram-bot).
<img width="965" height="654" alt="Screenshot 2026-01-07 at 1 04 54 PM" src="https://github.com/user-attachments/assets/66f10ca2-8915-46e9-92e6-fcc430d3e555" />


## Features

- **Session Management** - Multiple concurrent sessions with different working directories
- **Autonomy Levels** - Control what tools Claude can use (read-only → unsafe)
- **Live Updates** - See tool usage in real-time as Claude works
- **Git Integration** - Quick git commands via `/git`
- **Context Threading** - Reply to messages to continue that context

## Requirements

- Python 3.10+
- Anthropic account with Claude Code access
- Telegram bot token

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Save the API token

### 2. Get Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. Save your numeric user ID

### 3. Install on Raspberry Pi

```bash
# Clone or copy the project
cd telegram-claude-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env  # Add your tokens
```

### 4. Authenticate Claude

The Claude Agent SDK will prompt for authentication on first run:

```bash
python bot.py
```

Follow the authentication flow if prompted.

## Configuration

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs |
| `DEFAULT_CWD` | Default working directory |
| `SESSIONS_FILE` | Where to save sessions |

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize bot |
| `/help` | Show commands |
| `/new [path]` | New session in directory |
| `/session` | List sessions |
| `/session [id]` | Switch to session |
| `/auto [level]` | Set autonomy level |
| `/cwd` | Show current directory |
| `/cwd [path]` | Change directory |
| `/status` | Show bot status |
| `/git [args]` | Run git command |

## Autonomy Levels

| Level | Tools Allowed |
|-------|---------------|
| `read-only` | Read, Glob, Grep, WebFetch, WebSearch |
| `low` | Above + Task |
| `medium` | Above + Edit, Write, NotebookEdit |
| `high` | Above + Bash |
| `unsafe` | All tools (dangerous!) |

## Running as a Service

Create `/etc/systemd/system/telegram-claude.service`:

```ini
[Unit]
Description=Telegram Claude Bot
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/telegram-claude-bot
ExecStart=/home/pi/telegram-claude-bot/venv/bin/python bot.py
Restart=on-failure
EnvironmentFile=/home/pi/telegram-claude-bot/.env

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl enable telegram-claude
sudo systemctl start telegram-claude
sudo systemctl status telegram-claude
```

## Security Notes

- **Always set ALLOWED_USER_IDS** - Anyone with your bot token can otherwise control your Pi
- **Start with low autonomy** - Only increase when needed
- **Be careful with `unsafe` mode** - Claude can run any command

## License

MIT

