#!/usr/bin/env python3
"""
Telegram Claude Bot - Bridge Telegram to Claude Code via the Claude Agent SDK.
Inspired by github.com/factory-ben/droid-telegram-bot
"""

import os
import re
import json
import html
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction

from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ToolResultBlock

load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()]
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
SESSIONS_FILE = os.path.expanduser(os.getenv("SESSIONS_FILE", "~/.telegram-claude-sessions.json"))

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
# Silence noisy loggers
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Autonomy levels with their allowed tools
AUTONOMY_LEVELS = {
    "read-only": ["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
    "low": ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task"],
    "medium": ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Edit", "Write", "NotebookEdit"],
    "high": ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Edit", "Write", "NotebookEdit", "Bash"],
    "unsafe": None,  # All tools allowed
}


@dataclass
class Session:
    """Represents a Claude conversation session."""
    id: str
    cwd: str
    autonomy: str = "low"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_message_id: Optional[int] = None
    conversation_history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "autonomy": self.autonomy,
            "created_at": self.created_at,
            "last_message_id": self.last_message_id,
            "conversation_history": self.conversation_history[-10:],  # Keep last 10
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=data["id"],
            cwd=data["cwd"],
            autonomy=data.get("autonomy", "low"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            last_message_id=data.get("last_message_id"),
            conversation_history=data.get("conversation_history", []),
        )


class SessionManager:
    """Manages user sessions with persistence."""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.sessions: dict[int, dict[str, Session]] = {}  # user_id -> {session_id -> Session}
        self.active: dict[int, str] = {}  # user_id -> active session_id
        self._load()

    def _load(self):
        if self.filepath.exists():
            try:
                data = json.loads(self.filepath.read_text())
                for user_id, user_data in data.get("sessions", {}).items():
                    user_id = int(user_id)
                    self.sessions[user_id] = {
                        sid: Session.from_dict(s) for sid, s in user_data.items()
                    }
                self.active = {int(k): v for k, v in data.get("active", {}).items()}
            except Exception as e:
                logger.error(f"Failed to load sessions: {e}")

    def _save(self):
        try:
            data = {
                "sessions": {
                    str(uid): {sid: s.to_dict() for sid, s in sessions.items()}
                    for uid, sessions in self.sessions.items()
                },
                "active": {str(k): v for k, v in self.active.items()},
            }
            self.filepath.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")

    def create(self, user_id: int, cwd: str = DEFAULT_CWD) -> Session:
        session_id = datetime.now().strftime("%Y%m%d%H%M%S")
        session = Session(id=session_id, cwd=cwd)
        if user_id not in self.sessions:
            self.sessions[user_id] = {}
        self.sessions[user_id][session_id] = session
        self.active[user_id] = session_id
        self._save()
        return session

    def get_active(self, user_id: int) -> Optional[Session]:
        if user_id not in self.active:
            return None
        session_id = self.active[user_id]
        return self.sessions.get(user_id, {}).get(session_id)

    def get_or_create(self, user_id: int) -> Session:
        session = self.get_active(user_id)
        if not session:
            session = self.create(user_id)
        return session

    def switch(self, user_id: int, session_id: str) -> Optional[Session]:
        if user_id in self.sessions and session_id in self.sessions[user_id]:
            self.active[user_id] = session_id
            self._save()
            return self.sessions[user_id][session_id]
        return None

    def list_sessions(self, user_id: int) -> list[Session]:
        return list(self.sessions.get(user_id, {}).values())

    def update(self, user_id: int, session: Session):
        if user_id in self.sessions and session.id in self.sessions[user_id]:
            self.sessions[user_id][session.id] = session
            self._save()


# Global session manager
sessions = SessionManager(SESSIONS_FILE)

# Pending permission requests: {callback_id: {"tool_name": str, "tool_input": dict, ...}}
pending_permissions: dict[str, dict] = {}


def markdown_to_html(text: str) -> str:
    """Convert markdown to Telegram HTML format."""
    # Preserve code blocks before escaping (extract raw content)
    code_blocks = []
    def save_code_block(match):
        code_blocks.append(match.group(1))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(?:\w+)?\n?(.*?)```", save_code_block, text, flags=re.DOTALL)

    # Preserve inline code
    inline_codes = []
    def save_inline_code(match):
        inline_codes.append(match.group(1))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)

    # Escape HTML
    text = html.escape(text)

    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Italic
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)

    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{i}\x00", f"<code>{html.escape(code)}</code>")

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{html.escape(block)}</pre>")

    return text


def truncate_message(text: str, max_length: int = 4000) -> str:
    """Truncate message to Telegram's limit."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 20] + "\n\n[Truncated]"


def redact_sensitive(text: str) -> str:
    """Redact sensitive information from text before sending to Telegram."""
    # Telegram bot tokens (format: 123456789:ABC-DEF...)
    text = re.sub(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b', '[TELEGRAM_TOKEN_REDACTED]', text)

    # Generic API keys/tokens (common patterns)
    # AWS access keys
    text = re.sub(r'\bAKIA[0-9A-Z]{16}\b', '[AWS_KEY_REDACTED]', text)
    # AWS secret keys
    text = re.sub(r'\b[A-Za-z0-9/+=]{40}\b(?=.*(?:aws|secret|key))', '[AWS_SECRET_REDACTED]', text, flags=re.IGNORECASE)

    # GitHub tokens
    text = re.sub(r'\bghp_[A-Za-z0-9]{36}\b', '[GITHUB_TOKEN_REDACTED]', text)
    text = re.sub(r'\bgho_[A-Za-z0-9]{36}\b', '[GITHUB_TOKEN_REDACTED]', text)
    text = re.sub(r'\bghu_[A-Za-z0-9]{36}\b', '[GITHUB_TOKEN_REDACTED]', text)
    text = re.sub(r'\bghs_[A-Za-z0-9]{36}\b', '[GITHUB_TOKEN_REDACTED]', text)

    # Anthropic API keys
    text = re.sub(r'\bsk-ant-[A-Za-z0-9-]{40,}\b', '[ANTHROPIC_KEY_REDACTED]', text)

    # OpenAI API keys
    text = re.sub(r'\bsk-[A-Za-z0-9]{48}\b', '[OPENAI_KEY_REDACTED]', text)

    # Generic secret patterns in .env style (KEY=value)
    text = re.sub(
        r'((?:API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY)["\']?\s*[=:]\s*["\']?)([A-Za-z0-9_\-/+=]{16,})(["\']?)',
        r'\1[REDACTED]\3',
        text,
        flags=re.IGNORECASE
    )

    # JWT tokens
    text = re.sub(r'\beyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b', '[JWT_REDACTED]', text)

    # Database connection strings with passwords
    text = re.sub(
        r'((?:postgres|mysql|mongodb|redis)(?:ql)?://[^:]+:)([^@]+)(@)',
        r'\1[PASSWORD_REDACTED]\3',
        text,
        flags=re.IGNORECASE
    )

    return text


def format_tool_use(tool_name: str, tool_input: dict) -> str:
    """Format tool use for display."""
    if tool_name == "Read":
        return f"Read: {tool_input.get('file_path', '?')}"
    elif tool_name == "Write":
        return f"Write: {tool_input.get('file_path', '?')}"
    elif tool_name == "Edit":
        return f"Edit: {tool_input.get('file_path', '?')}"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "?")
        if len(cmd) > 50:
            cmd = cmd[:47] + "..."
        return f"Bash: {cmd}"
    elif tool_name == "Glob":
        return f"Glob: {tool_input.get('pattern', '?')}"
    elif tool_name == "Grep":
        return f"Grep: {tool_input.get('pattern', '?')}"
    elif tool_name == "WebSearch":
        return f"WebSearch: '{tool_input.get('query', '?')}'"
    elif tool_name == "WebFetch":
        return f"WebFetch: {tool_input.get('url', '?')}"
    elif tool_name == "Task":
        return f"Task: {tool_input.get('description', '?')}"
    else:
        return f"{tool_name}"


def get_git_info(cwd: str) -> str:
    """Get git branch and status for a directory."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return ""
        branch = result.stdout.strip()

        # Count uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5
        )
        changes = len([l for l in result.stdout.strip().split("\n") if l])

        if changes > 0:
            return f"({branch}, {changes} uncommitted)"
        return f"({branch})"
    except Exception:
        return ""


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    if not ALLOWED_USER_IDS:
        return True  # No restrictions if not configured
    return user_id in ALLOWED_USER_IDS


# Command handlers

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = update.effective_user.id
    session = sessions.get_or_create(user_id)

    await update.message.reply_text(
        f"Claude Code Bridge ready.\n\n"
        f"Session: <code>{session.id[:8]}</code>\n"
        f"Directory: <code>{session.cwd}</code>\n"
        f"Autonomy: {session.autonomy}\n\n"
        f"Send any message to chat with Claude.\n"
        f"Use /help for commands.",
        parse_mode=ParseMode.HTML
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    if not is_authorized(update.effective_user.id):
        return

    await update.message.reply_text(
        "<b>Commands</b>\n\n"
        "/start - Initialize bot\n"
        "/new [path] - New session in directory\n"
        "/session - List sessions\n"
        "/session [id] - Switch to session\n"
        "/auto [level] - Set autonomy level\n"
        "/cwd - Show current directory\n"
        "/cwd [path] - Change directory\n"
        "/status - Show bot status\n"
        "/git [args] - Run git command\n\n"
        "<b>Autonomy Levels</b>\n"
        "read-only, low, medium, high, unsafe\n\n"
        "<b>Tips</b>\n"
        "Reply to a message to continue that context.",
        parse_mode=ParseMode.HTML
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /new command - create new session."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    cwd = " ".join(context.args) if context.args else DEFAULT_CWD
    cwd = os.path.expanduser(cwd)

    if not os.path.isdir(cwd):
        await update.message.reply_text(f"Directory not found: {cwd}")
        return

    session = sessions.create(user_id, cwd)
    git_info = get_git_info(cwd)

    await update.message.reply_text(
        f"New session created.\n\n"
        f"Session: <code>{session.id[:8]}</code>\n"
        f"Directory: <code>{cwd}</code> {git_info}",
        parse_mode=ParseMode.HTML
    )


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /session command - list or switch sessions."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id

    if context.args:
        # Switch to session
        session_id = context.args[0]
        # Try to find session by prefix
        user_sessions = sessions.list_sessions(user_id)
        matches = [s for s in user_sessions if s.id.startswith(session_id)]

        if len(matches) == 1:
            session = sessions.switch(user_id, matches[0].id)
            await update.message.reply_text(
                f"Switched to session <code>{session.id[:8]}</code>\n"
                f"Directory: <code>{session.cwd}</code>",
                parse_mode=ParseMode.HTML
            )
        elif len(matches) > 1:
            await update.message.reply_text("Ambiguous session ID. Be more specific.")
        else:
            await update.message.reply_text("Session not found.")
    else:
        # List sessions
        user_sessions = sessions.list_sessions(user_id)
        active = sessions.get_active(user_id)

        if not user_sessions:
            await update.message.reply_text("No sessions. Use /new to create one.")
            return

        lines = ["<b>Sessions</b>\n"]
        for s in sorted(user_sessions, key=lambda x: x.created_at, reverse=True)[:10]:
            marker = "→ " if active and s.id == active.id else "  "
            lines.append(
                f"{marker}<code>{s.id[:8]}</code> {s.cwd} [{s.autonomy}]"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /auto command - set autonomy level."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    session = sessions.get_or_create(user_id)

    if not context.args:
        await update.message.reply_text(
            f"Current autonomy: <b>{session.autonomy}</b>\n\n"
            f"Levels: read-only, low, medium, high, unsafe\n"
            f"Usage: /auto [level]",
            parse_mode=ParseMode.HTML
        )
        return

    level = context.args[0].lower()
    if level not in AUTONOMY_LEVELS:
        await update.message.reply_text(
            f"Invalid level. Choose: {', '.join(AUTONOMY_LEVELS.keys())}"
        )
        return

    session.autonomy = level
    sessions.update(user_id, session)

    await update.message.reply_text(
        f"Autonomy set to <b>{level}</b>",
        parse_mode=ParseMode.HTML
    )


async def cmd_cwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cwd command - show or change directory."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    session = sessions.get_or_create(user_id)

    if context.args:
        new_cwd = os.path.expanduser(" ".join(context.args))
        if not os.path.isdir(new_cwd):
            await update.message.reply_text(f"Directory not found: {new_cwd}")
            return
        session.cwd = new_cwd
        sessions.update(user_id, session)

    git_info = get_git_info(session.cwd)
    await update.message.reply_text(
        f"<code>{session.cwd}</code> {git_info}",
        parse_mode=ParseMode.HTML
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    session = sessions.get_active(user_id)

    if not session:
        await update.message.reply_text("No active session. Use /start")
        return

    git_info = get_git_info(session.cwd)

    await update.message.reply_text(
        f"<b>Status</b>\n\n"
        f"Session: <code>{session.id[:8]}</code>\n"
        f"Directory: <code>{session.cwd}</code> {git_info}\n"
        f"Autonomy: {session.autonomy}\n"
        f"Created: {session.created_at[:10]}",
        parse_mode=ParseMode.HTML
    )


async def cmd_git(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /git command - run git commands."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    session = sessions.get_or_create(user_id)

    if not context.args:
        await update.message.reply_text("Usage: /git [command]\nExample: /git status")
        return

    import subprocess
    try:
        result = subprocess.run(
            ["git"] + list(context.args),
            cwd=session.cwd,
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout or result.stderr or "(no output)"
        output = truncate_message(output, 3900)
        await update.message.reply_text(f"<pre>{html.escape(output)}</pre>", parse_mode=ParseMode.HTML)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Command timed out.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - send to Claude."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = update.effective_user.id
    user_message = update.message.text

    if not user_message:
        return

    session = sessions.get_or_create(user_id)

    # Send typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Build status message for streaming updates
    status_message = await update.message.reply_text(
        f"Working...\n\n<code>{session.cwd}</code> {get_git_info(session.cwd)}",
        parse_mode=ParseMode.HTML
    )

    try:
        # Configure Claude options based on autonomy level
        allowed_tools = AUTONOMY_LEVELS.get(session.autonomy)

        options = ClaudeAgentOptions(
            cwd=session.cwd,
            allowed_tools=allowed_tools,
            max_turns=10,
        )

        # Collect response
        full_response = ""
        tool_uses = []
        last_update = 0

        async for message in query(prompt=user_message, options=options):

            # Handle different message types
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_response += block.text
                    elif isinstance(block, ToolUseBlock):
                        tool_info = format_tool_use(block.name, block.input)
                        tool_uses.append(tool_info)

                        # Update status with tool usage (max 5 shown)
                        tools_text = "\n".join(f"→ {t}" for t in tool_uses[-5:])
                        try:
                            # Throttle updates
                            now = asyncio.get_event_loop().time()
                            if now - last_update > 1.0:
                                await status_message.edit_text(
                                    f"Working...\n\n{tools_text}",
                                    parse_mode=ParseMode.HTML
                                )
                                last_update = now
                        except Exception:
                            pass

            # Handle ResultMessage (final response)
            elif isinstance(message, ResultMessage):
                logger.info(f"ResultMessage attrs: {dir(message)}")
                logger.info(f"ResultMessage: {message}")
                if hasattr(message, 'result') and message.result:
                    full_response = str(message.result)
                elif hasattr(message, 'text') and message.text:
                    full_response = str(message.text)

        # Delete status message
        try:
            await status_message.delete()
        except Exception:
            pass

        # Send final response
        if full_response:
            # Redact sensitive info before processing
            full_response = redact_sensitive(full_response)
            response_html = markdown_to_html(full_response)
            response_html = truncate_message(response_html)

            # Add context header
            git_info = get_git_info(session.cwd)
            header = f"<code>{session.cwd}</code> {git_info}\n\n"

            sent_message = await update.message.reply_text(
                header + response_html,
                parse_mode=ParseMode.HTML
            )

            # Update session
            session.last_message_id = sent_message.message_id
            sessions.update(user_id, session)
        else:
            await update.message.reply_text("No response from Claude.")

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        try:
            await status_message.delete()
        except Exception:
            pass
        await update.message.reply_text(f"Error: {str(e)[:200]}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for permissions."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("perm_"):
        parts = data.split("_")
        if len(parts) >= 3:
            action = parts[1]  # once, always, deny
            callback_id = parts[2]

            if callback_id in pending_permissions:
                perm = pending_permissions.pop(callback_id)
                if action == "deny":
                    await query.edit_message_text(
                        f"Denied: {perm.get('tool_name', 'Unknown')}"
                    )
                elif action == "once":
                    await query.edit_message_text(
                        f"Allowed once: {perm.get('tool_name', 'Unknown')}"
                    )
                elif action == "always":
                    await query.edit_message_text(
                        f"Always allowed: {perm.get('tool_name', 'Unknown')}"
                    )


def main():
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        print("Create a .env file with your bot token")
        return

    # Build application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running...")
    print(f"Allowed users: {ALLOWED_USER_IDS or 'All'}")
    print(f"Default directory: {DEFAULT_CWD}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
