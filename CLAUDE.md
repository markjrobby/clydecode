# CLAUDE.md

## Project: Clyde Code

A Telegram bot that bridges to Claude Code CLI, enabling mobile coding workflows.

## Critical Security Rules

**This repo is PUBLIC on GitHub. Never commit or push:**

- API keys (Anthropic, Telegram, etc.)
- Bot tokens
- User IDs
- Passwords or secrets
- `.env` files (only `.env.example` with placeholders)
- Session files
- Any personal/identifiable information

**Before every commit, verify:**
1. No secrets in code or config files
2. `.gitignore` includes `.env`, `*.json` session files
3. Example files use placeholder values only

## File Overview

- `bot.py` - Main Telegram bot
- `requirements.txt` - Python dependencies
- `.env.example` - Template config (placeholders only)
- `docs/` - Documentation and PRDs
- `mini-app/` - Telegram Mini App for diff viewing

## Commands

```bash
# Run the bot
python bot.py

# Install dependencies
pip install -r requirements.txt
```

## Architecture

```
User (Telegram) → bot.py → Claude Code CLI → Codebase
                    ↓
              Mini App (Vercel) for diff viewing
```

## Current State

- [x] Basic Telegram bot working
- [x] Claude CLI integration with streaming
- [x] Session management
- [ ] Approval workflow for edits
- [ ] Mini App diff viewer
