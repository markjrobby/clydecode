#!/usr/bin/env python3
"""
Telegram Claude Bot - Bridge Telegram to Claude Code CLI.
Runs Claude Code directly on the Raspberry Pi for full local context.
"""

import os
import re
import json
import html
import asyncio
import logging
import subprocess
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

load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()]
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
SESSIONS_FILE = os.path.expanduser(os.getenv("SESSIONS_FILE", "~/.telegram-claude-sessions.json"))

# Logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass
class Session:
    """Represents a Claude conversation session."""
    id: str
    cwd: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    resume_id: Optional[str] = None  # Claude session ID for conversation continuity

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "resume_id": self.resume_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=data["id"],
            cwd=data["cwd"],
            created_at=data.get("created_at", datetime.now().isoformat()),
            resume_id=data.get("resume_id"),
        )


class SessionManager:
    """Manages user sessions with persistence."""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.sessions: dict[int, Session] = {}
        self._load()

    def _load(self):
        if self.filepath.exists():
            try:
                data = json.loads(self.filepath.read_text())
                for uid, s in data.items():
                    self.sessions[int(uid)] = Session.from_dict(s)
            except Exception as e:
                logger.error(f"Failed to load sessions: {e}")

    def _save(self):
        try:
            data = {str(k): v.to_dict() for k, v in self.sessions.items()}
            self.filepath.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")

    def get_or_create(self, user_id: int) -> Session:
        if user_id not in self.sessions:
            self.sessions[user_id] = Session(
                id=datetime.now().strftime("%Y%m%d%H%M%S"),
                cwd=DEFAULT_CWD
            )
            self._save()
        return self.sessions[user_id]

    def update(self, user_id: int, **kwargs):
        session = self.get_or_create(user_id)
        for k, v in kwargs.items():
            setattr(session, k, v)
        self._save()

    def reset(self, user_id: int, cwd: str = None) -> Session:
        """Create a new session, clearing conversation history."""
        self.sessions[user_id] = Session(
            id=datetime.now().strftime("%Y%m%d%H%M%S"),
            cwd=cwd or DEFAULT_CWD
        )
        self._save()
        return self.sessions[user_id]


sessions = SessionManager(SESSIONS_FILE)

# Pending permission requests: {callback_id: {process, prompt_data, chat_id, ...}}
pending_permissions: dict[str, dict] = {}


def redact_sensitive(text: str) -> str:
    """Redact sensitive information from text before sending to Telegram."""
    # Telegram bot tokens
    text = re.sub(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b', '[TELEGRAM_TOKEN_REDACTED]', text)
    # AWS keys
    text = re.sub(r'\bAKIA[0-9A-Z]{16}\b', '[AWS_KEY_REDACTED]', text)
    # GitHub tokens
    text = re.sub(r'\bgh[pous]_[A-Za-z0-9]{36}\b', '[GITHUB_TOKEN_REDACTED]', text)
    # Anthropic API keys
    text = re.sub(r'\bsk-ant-[A-Za-z0-9-]{40,}\b', '[ANTHROPIC_KEY_REDACTED]', text)
    # OpenAI API keys
    text = re.sub(r'\bsk-[A-Za-z0-9]{48}\b', '[OPENAI_KEY_REDACTED]', text)
    # Generic secrets in .env style
    text = re.sub(
        r'((?:API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY)["\']?\s*[=:]\s*["\']?)([A-Za-z0-9_\-/+=]{16,})(["\']?)',
        r'\1[REDACTED]\3',
        text,
        flags=re.IGNORECASE
    )
    # JWT tokens
    text = re.sub(r'\beyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b', '[JWT_REDACTED]', text)
    # Database URLs with passwords
    text = re.sub(
        r'((?:postgres|mysql|mongodb|redis)(?:ql)?://[^:]+:)([^@]+)(@)',
        r'\1[REDACTED]\3',
        text,
        flags=re.IGNORECASE
    )
    return text


def markdown_to_html(text: str) -> str:
    """Convert markdown to Telegram HTML format."""
    code_blocks = []
    def save_code_block(match):
        code_blocks.append(match.group(1))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(?:\w+)?\n?(.*?)```", save_code_block, text, flags=re.DOTALL)

    inline_codes = []
    def save_inline_code(match):
        inline_codes.append(match.group(1))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{i}\x00", f"<code>{html.escape(code)}</code>")
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{html.escape(block)}</pre>")

    return text


def truncate_message(text: str, max_length: int = 4000) -> str:
    """Truncate message to Telegram's limit."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 20] + "\n\n[Truncated]"


def get_git_info(cwd: str) -> str:
    """Get git branch and status for a directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return ""
        branch = result.stdout.strip()
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=5
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
        return True
    return user_id in ALLOWED_USER_IDS


def format_tool_use(tool_name: str, tool_input: dict) -> str:
    """Format tool use for display."""
    if tool_name == "Read":
        path = tool_input.get('file_path', '?')
        return f"Reading: {path.split('/')[-1]}"
    elif tool_name == "Write":
        path = tool_input.get('file_path', '?')
        return f"Writing: {path.split('/')[-1]}"
    elif tool_name == "Edit":
        path = tool_input.get('file_path', '?')
        return f"Editing: {path.split('/')[-1]}"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "?")
        if len(cmd) > 40:
            cmd = cmd[:37] + "..."
        return f"Running: {cmd}"
    elif tool_name == "Glob":
        return f"Searching: {tool_input.get('pattern', '?')}"
    elif tool_name == "Grep":
        return f"Grep: {tool_input.get('pattern', '?')}"
    elif tool_name == "WebSearch":
        return f"Searching web: {tool_input.get('query', '?')[:30]}"
    elif tool_name == "WebFetch":
        url = tool_input.get('url', '?')
        return f"Fetching: {url[:40]}..."
    elif tool_name == "Task":
        return f"Task: {tool_input.get('description', '?')[:30]}"
    else:
        return f"{tool_name}"


def format_diff(old_string: str, new_string: str, file_path: str) -> str:
    """Format a simple diff for display."""
    filename = file_path.split('/')[-1]

    # Truncate if too long
    max_len = 800
    old_display = old_string[:max_len] + "..." if len(old_string) > max_len else old_string
    new_display = new_string[:max_len] + "..." if len(new_string) > max_len else new_string

    diff_text = f"<b>File:</b> {filename}\n\n"
    diff_text += f"<b>- Remove:</b>\n<pre>{html.escape(old_display)}</pre>\n\n"
    diff_text += f"<b>+ Add:</b>\n<pre>{html.escape(new_display)}</pre>"

    return diff_text


async def run_claude_streaming(prompt: str, cwd: str, status_message, context, chat_id: int, resume_id: str = None):
    """
    Run Claude Code CLI with streaming output.
    Updates status_message with progress.
    Returns (response_text, session_id).
    """
    # Use allowedTools to let Claude work, edits will be shown as diffs
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json"]

    if resume_id:
        cmd.extend(["--resume", resume_id])

    logger.info(f"Running Claude in {cwd}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    )

    full_response = ""
    session_id = None
    tool_uses = []
    last_update = 0
    git_info = get_git_info(cwd)
    edits_made = []  # Track edits for summary

    try:
        while True:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=300
            )

            if not line:
                break

            try:
                data = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # Handle different message types
            if msg_type == "assistant":
                # Assistant text response
                content = data.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        full_response = block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        tool_uses.append(format_tool_use(tool_name, tool_input))

                        # Show diff for Edit operations
                        if tool_name == "Edit" and tool_input.get("old_string") and tool_input.get("new_string"):
                            diff_display = format_diff(
                                tool_input.get("old_string", ""),
                                tool_input.get("new_string", ""),
                                tool_input.get("file_path", "unknown")
                            )
                            edits_made.append(tool_input.get("file_path", "unknown").split("/")[-1])

                            # Send diff as a separate message
                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"📝 <b>Edit:</b>\n\n{diff_display}",
                                    parse_mode=ParseMode.HTML
                                )
                            except Exception as e:
                                logger.error(f"Failed to send diff: {e}")

                        # Show diff for Write operations (new files)
                        elif tool_name == "Write" and tool_input.get("content"):
                            file_path = tool_input.get("file_path", "unknown")
                            content_preview = tool_input.get("content", "")[:1000]
                            if len(tool_input.get("content", "")) > 1000:
                                content_preview += "..."
                            edits_made.append(file_path.split("/")[-1])

                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"📄 <b>New File:</b> {file_path.split('/')[-1]}\n\n<pre>{html.escape(content_preview)}</pre>",
                                    parse_mode=ParseMode.HTML
                                )
                            except Exception as e:
                                logger.error(f"Failed to send write preview: {e}")

                        # Update status message with tool activity
                        now = asyncio.get_event_loop().time()
                        if now - last_update > 0.8:  # Throttle updates
                            tools_display = "\n".join(f"→ {t}" for t in tool_uses[-5:])
                            try:
                                await status_message.edit_text(
                                    f"<code>{cwd}</code> {git_info}\n\n{tools_display}",
                                    parse_mode=ParseMode.HTML
                                )
                                last_update = now
                            except Exception:
                                pass

            elif msg_type == "result":
                # Final result
                full_response = data.get("result", full_response)
                session_id = data.get("session_id")

                # Add edit summary to response if edits were made
                if edits_made:
                    files_summary = ", ".join(set(edits_made))
                    full_response = f"✅ <b>Files modified:</b> {files_summary}\n\n{full_response}"

            elif msg_type == "system":
                # System message (initial context loading)
                now = asyncio.get_event_loop().time()
                if now - last_update > 1.0:
                    try:
                        await status_message.edit_text(
                            f"<code>{cwd}</code> {git_info}\n\nLoading context...",
                            parse_mode=ParseMode.HTML
                        )
                        last_update = now
                    except Exception:
                        pass

    except asyncio.TimeoutError:
        process.kill()
        return "Request timed out after 5 minutes.", None

    await process.wait()

    if process.returncode != 0:
        stderr = await process.stderr.read()
        error_msg = stderr.decode().strip() if stderr else "Unknown error"
        logger.error(f"Claude CLI error: {error_msg}")
        return f"Error: {error_msg[:500]}", None

    return full_response, session_id


# Command handlers

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    session = sessions.get_or_create(update.effective_user.id)
    git_info = get_git_info(session.cwd)

    await update.message.reply_text(
        f"Claude Code Bot ready.\n\n"
        f"Session: <code>{session.id}</code>\n"
        f"Directory: <code>{session.cwd}</code> {git_info}\n\n"
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
        "/cwd - Show current directory\n"
        "/cwd [path] - Change directory\n"
        "/status - Show bot status\n"
        "/git [args] - Run git command\n\n"
        "<b>Tips</b>\n"
        "Use /new to reset conversation context.",
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

    session = sessions.reset(user_id, cwd)
    git_info = get_git_info(cwd)

    await update.message.reply_text(
        f"New session created.\n\n"
        f"Session: <code>{session.id}</code>\n"
        f"Directory: <code>{cwd}</code> {git_info}",
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
        sessions.update(user_id, cwd=new_cwd)
        session.cwd = new_cwd

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
    session = sessions.get_or_create(user_id)
    git_info = get_git_info(session.cwd)

    await update.message.reply_text(
        f"<b>Status</b>\n\n"
        f"Session: <code>{session.id}</code>\n"
        f"Directory: <code>{session.cwd}</code> {git_info}\n"
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

    try:
        result = subprocess.run(
            ["git"] + list(context.args),
            cwd=session.cwd,
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout or result.stderr or "(no output)"
        output = redact_sensitive(output)
        output = truncate_message(output, 3900)
        await update.message.reply_text(f"<pre>{html.escape(output)}</pre>", parse_mode=ParseMode.HTML)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Command timed out.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - send to Claude Code CLI."""
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

    # Send working status
    git_info = get_git_info(session.cwd)
    status_message = await update.message.reply_text(
        f"Working...\n\n<code>{session.cwd}</code> {git_info}",
        parse_mode=ParseMode.HTML
    )

    try:
        # Run Claude Code CLI with streaming
        response, new_session_id = await run_claude_streaming(
            prompt=user_message,
            cwd=session.cwd,
            status_message=status_message,
            context=context,
            chat_id=update.effective_chat.id,
            resume_id=session.resume_id
        )

        # Update session with new resume ID if provided
        if new_session_id:
            sessions.update(user_id, resume_id=new_session_id)

        # Delete status message
        try:
            await status_message.delete()
        except Exception:
            pass

        # Send response
        if response:
            response = redact_sensitive(response)
            response_html = markdown_to_html(response)
            response_html = truncate_message(response_html)

            header = f"<code>{session.cwd}</code> {git_info}\n\n"

            await update.message.reply_text(
                header + response_html,
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text("No response from Claude.")

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        try:
            await status_message.delete()
        except Exception:
            pass
        await update.message.reply_text(f"Error: {str(e)[:200]}")


def main():
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"Bot running! Using Claude Code CLI directly.")
    print(f"Allowed users: {ALLOWED_USER_IDS or 'All'}")
    print(f"Default directory: {DEFAULT_CWD}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
