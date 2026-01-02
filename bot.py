#!/usr/bin/env python3
"""
Telegram Claude Bot - Bridge Telegram to Claude Code CLI.
Runs Claude Code directly on the Raspberry Pi for full local context.
"""

import os
import re
import json
import html
import shutil
import asyncio
import logging
import subprocess
import uuid
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


@dataclass
class PendingEdit:
    """Tracks an edit waiting for user approval."""
    edit_id: str
    chat_id: int
    message_id: int
    user_id: int
    tool_name: str  # "Edit" or "Write"
    file_path: str
    old_string: str  # Original content (empty for Write)
    new_string: str  # New content
    process: asyncio.subprocess.Process
    session_id: Optional[str]
    cwd: str
    created_at: datetime = field(default_factory=datetime.now)


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

# Pending edits awaiting approval: {edit_id: PendingEdit}
pending_edits: dict[str, PendingEdit] = {}


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
    """Format a clean diff showing removed and added lines."""
    filename = file_path.split('/')[-1]

    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()

    # If the strings are identical, no changes
    if old_string == new_string:
        return f"<b>📄 {filename}</b>\n\n<i>No changes</i>"

    formatted_lines = []

    # Show removed lines (old content)
    if old_lines:
        for line in old_lines:
            formatted_lines.append(f"- {line}")

    # Add separator if both old and new content exist
    if old_lines and new_lines:
        formatted_lines.append("")

    # Show added lines (new content)
    if new_lines:
        for line in new_lines:
            formatted_lines.append(f"+ {line}")

    diff_content = '\n'.join(formatted_lines)

    # Truncate if too long for Telegram
    max_len = 2500
    if len(diff_content) > max_len:
        diff_content = diff_content[:max_len] + "\n\n... (truncated)"

    diff_text = f"<b>📄 {filename}</b>\n\n"
    diff_text += f"<code>{html.escape(diff_content)}</code>"

    return diff_text


def format_new_file(content: str, file_path: str) -> str:
    """Format new file preview."""
    filename = file_path.split('/')[-1]

    max_len = 2500
    display = content[:max_len]
    if len(content) > max_len:
        display += "\n\n... (truncated)"

    lines = display.split('\n')
    formatted = '\n'.join(f"+ {line}" for line in lines)

    text = f"<b>📄 {filename}</b> (new file)\n\n"
    text += f"<code>{html.escape(formatted)}</code>"

    return text


async def run_claude_streaming(
    prompt: str,
    cwd: str,
    status_message,
    context,
    chat_id: int,
    user_id: int,
    resume_id: str = None
) -> dict:
    """
    Run Claude Code CLI with streaming output.
    Updates status_message with progress.

    Returns dict with one of:
    - {"status": "complete", "response": str, "session_id": str}
    - {"status": "pending_approval", "edit_id": str, "tool_name": str, ...}
    - {"status": "error", "message": str}
    """
    # Use stream-json for real-time updates
    # --verbose is REQUIRED when using stream-json with -p
    base_cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]

    if resume_id:
        base_cmd.extend(["--resume", resume_id])

    cmd = base_cmd
    logger.info(f"Running Claude in {cwd}")

    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

    # Don't pipe stdin - Claude CLI doesn't need it and it may cause issues
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=env
    )

    full_response = ""
    session_id = None
    tool_uses = []
    last_update = [0.0]  # Use list for mutable in closure
    git_info = get_git_info(cwd)
    edits_made = []  # Track edits for summary
    stop_heartbeat = [False]

    async def update_status(text: str, force: bool = False):
        """Update the status message with throttling."""
        now = asyncio.get_event_loop().time()
        if force or now - last_update[0] > 1.0:
            try:
                await status_message.edit_text(
                    f"<code>{cwd}</code> {git_info}\n\n{text}",
                    parse_mode=ParseMode.HTML
                )
                last_update[0] = now
            except Exception:
                pass

    async def heartbeat():
        """Show progress animation while processing."""
        phases = ["⏳ Analyzing request...", "⏳ Reading context...", "⏳ Processing...", "⏳ Thinking..."]
        i = 0
        while not stop_heartbeat[0]:
            if not tool_uses:  # Only animate if no tool activity yet
                await update_status(phases[i % len(phases)], force=True)
            i += 1
            await asyncio.sleep(2)

    # Start heartbeat task
    heartbeat_task = asyncio.create_task(heartbeat())

    async def read_stderr():
        """Read stderr in background and log it."""
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if line_str:
                    logger.warning(f"Claude stderr: {line_str}")
        except Exception as e:
            logger.error(f"stderr reader error: {e}")

    # Start stderr reader
    stderr_task = asyncio.create_task(read_stderr())

    try:
        buffer = ""
        while True:
            try:
                # Read chunks instead of lines to handle buffering better
                chunk = await asyncio.wait_for(
                    process.stdout.read(4096),
                    timeout=300
                )
            except asyncio.TimeoutError:
                break

            if not chunk:
                break

            buffer += chunk.decode()

            # Process complete lines
            while "\n" in buffer:
                line_str, buffer = buffer.split("\n", 1)
                line_str = line_str.strip()

                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
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
                            tool_display = format_tool_use(tool_name, tool_input)
                            tool_uses.append(tool_display)

                            # Update status immediately for each tool
                            tools_display = "\n".join(f"→ {t}" for t in tool_uses[-5:])
                            await update_status(tools_display, force=True)

                            # Pause for Edit operations - require approval
                            if tool_name == "Edit":
                                stop_heartbeat[0] = True
                                heartbeat_task.cancel()
                                stderr_task.cancel()

                                edit_id = str(uuid.uuid4())[:8]
                                return {
                                    "status": "pending_approval",
                                    "edit_id": edit_id,
                                    "tool_name": "Edit",
                                    "file_path": tool_input.get("file_path", ""),
                                    "old_string": tool_input.get("old_string", ""),
                                    "new_string": tool_input.get("new_string", ""),
                                    "process": process,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "user_id": user_id,
                                    "status_message": status_message,
                                }

                            # Pause for Write operations - require approval
                            elif tool_name == "Write":
                                stop_heartbeat[0] = True
                                heartbeat_task.cancel()
                                stderr_task.cancel()

                                edit_id = str(uuid.uuid4())[:8]
                                return {
                                    "status": "pending_approval",
                                    "edit_id": edit_id,
                                    "tool_name": "Write",
                                    "file_path": tool_input.get("file_path", ""),
                                    "old_string": "",
                                    "new_string": tool_input.get("content", ""),
                                    "process": process,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "user_id": user_id,
                                    "status_message": status_message,
                                }

                elif msg_type == "user":
                    # Tool results - show brief status
                    content = data.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "tool_result":
                            if tool_uses and not tool_uses[-1].startswith("✓"):
                                tool_uses.append("✓ Done")
                                tools_display = "\n".join(f"→ {t}" for t in tool_uses[-5:])
                                await update_status(tools_display)

                elif msg_type == "result":
                    # Final result
                    full_response = data.get("result", full_response)
                    session_id = data.get("session_id")

                    if edits_made:
                        files_summary = ", ".join(set(edits_made))
                        full_response = f"✅ <b>Files modified:</b> {files_summary}\n\n{full_response}"

                elif msg_type == "system":
                    # Extract session_id from system message
                    session_id = data.get("session_id", session_id)
                    tool_uses.append("📂 Loading context...")
                    await update_status("📂 Loading context...", force=True)

    except asyncio.TimeoutError:
        stop_heartbeat[0] = True
        heartbeat_task.cancel()
        process.kill()
        return {"status": "error", "message": "Request timed out after 5 minutes."}

    finally:
        stop_heartbeat[0] = True
        heartbeat_task.cancel()
        stderr_task.cancel()

    await process.wait()

    if process.returncode != 0:
        stderr = await process.stderr.read()
        error_msg = stderr.decode().strip() if stderr else "Unknown error"
        logger.error(f"Claude CLI error: {error_msg}")
        return {"status": "error", "message": f"Error: {error_msg[:500]}"}

    return {"status": "complete", "response": full_response, "session_id": session_id}


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


async def show_approval_request(chat_id: int, context, edit_info: dict) -> int:
    """Show diff with approval buttons. Returns the message ID."""
    edit_id = edit_info["edit_id"]

    # Format diff
    if edit_info["tool_name"] == "Edit":
        diff_text = format_diff(
            edit_info["old_string"],
            edit_info["new_string"],
            edit_info["file_path"]
        )
        btn_approve, btn_reject = "✅ Approve", "❌ Reject"
    else:
        diff_text = format_new_file(
            edit_info["new_string"],
            edit_info["file_path"]
        )
        btn_approve, btn_reject = "✅ Create", "❌ Cancel"

    # Create buttons
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(btn_approve, callback_data=f"approve_{edit_id}"),
        InlineKeyboardButton(btn_reject, callback_data=f"reject_{edit_id}")
    ]])

    # Send diff with buttons
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=diff_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    return msg.message_id


async def continue_after_approval(pending: PendingEdit, context) -> dict:
    """Continue reading Claude output after approval."""
    full_response = ""
    session_id = pending.session_id

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    pending.process.stdout.read(4096),
                    timeout=300
                )
            except asyncio.TimeoutError:
                break

            if not chunk:
                break

            for line in chunk.decode().split('\n'):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    msg_type = data.get("type")

                    if msg_type == "result":
                        full_response = data.get("result", "")
                        session_id = data.get("session_id", session_id)

                    elif msg_type == "assistant":
                        content = data.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                tool_input = block.get("input", {})

                                # Another Edit/Write - need approval again
                                if tool_name == "Edit":
                                    edit_id = str(uuid.uuid4())[:8]
                                    return {
                                        "status": "pending_approval",
                                        "edit_id": edit_id,
                                        "tool_name": "Edit",
                                        "file_path": tool_input.get("file_path", ""),
                                        "old_string": tool_input.get("old_string", ""),
                                        "new_string": tool_input.get("new_string", ""),
                                        "process": pending.process,
                                        "session_id": session_id,
                                        "cwd": pending.cwd,
                                        "user_id": pending.user_id,
                                    }
                                elif tool_name == "Write":
                                    edit_id = str(uuid.uuid4())[:8]
                                    return {
                                        "status": "pending_approval",
                                        "edit_id": edit_id,
                                        "tool_name": "Write",
                                        "file_path": tool_input.get("file_path", ""),
                                        "old_string": "",
                                        "new_string": tool_input.get("content", ""),
                                        "process": pending.process,
                                        "session_id": session_id,
                                        "cwd": pending.cwd,
                                        "user_id": pending.user_id,
                                    }

                except json.JSONDecodeError:
                    continue

        await pending.process.wait()
        return {"status": "complete", "response": full_response, "session_id": session_id}

    except Exception as e:
        logger.error(f"Error continuing after approval: {e}")
        return {"status": "error", "message": str(e)}


async def handle_approve(query, pending: PendingEdit, context):
    """Handle approval of an edit."""
    filename = pending.file_path.split('/')[-1]

    # Update the diff message to show approval
    await query.edit_message_text(
        f"✅ Approved edit to <b>{filename}</b>\n\nContinuing...",
        parse_mode=ParseMode.HTML
    )

    # Continue reading Claude output
    result = await continue_after_approval(pending, context)

    if result["status"] == "pending_approval":
        # Another edit needs approval
        msg_id = await show_approval_request(pending.chat_id, context, result)

        pending_edits[result["edit_id"]] = PendingEdit(
            edit_id=result["edit_id"],
            chat_id=pending.chat_id,
            message_id=msg_id,
            user_id=pending.user_id,
            tool_name=result["tool_name"],
            file_path=result["file_path"],
            old_string=result["old_string"],
            new_string=result["new_string"],
            process=result["process"],
            session_id=result["session_id"],
            cwd=pending.cwd,
        )

    elif result["status"] == "complete":
        # Update session with new session ID
        if result.get("session_id"):
            sessions.update(pending.user_id, resume_id=result["session_id"])

        if result.get("response"):
            response = redact_sensitive(result["response"])
            response_html = markdown_to_html(response)
            response_html = truncate_message(response_html)

            git_info = get_git_info(pending.cwd)
            header = f"<code>{pending.cwd}</code> {git_info}\n\n"

            await context.bot.send_message(
                chat_id=pending.chat_id,
                text=header + response_html,
                parse_mode=ParseMode.HTML
            )

    elif result["status"] == "error":
        await context.bot.send_message(
            chat_id=pending.chat_id,
            text=f"Error: {result.get('message', 'Unknown error')[:200]}"
        )


async def handle_reject(query, pending: PendingEdit, context):
    """Handle rejection of an edit."""
    filename = pending.file_path.split('/')[-1]

    # Kill Claude process
    try:
        pending.process.kill()
        await pending.process.wait()
    except Exception:
        pass

    # Prompt for new instructions
    await query.edit_message_text(
        f"❌ Rejected edit to <b>{filename}</b>\n\n"
        f"What would you like me to do instead?",
        parse_mode=ParseMode.HTML
    )


async def handle_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle approve/reject button callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "approve_abc123" or "reject_abc123"
    action, edit_id = data.split("_", 1)

    if edit_id not in pending_edits:
        await query.edit_message_text("⚠️ This edit has expired.")
        return

    pending = pending_edits.pop(edit_id)

    # Verify user
    if query.from_user.id != pending.user_id:
        pending_edits[edit_id] = pending  # Put back
        await query.answer("Only the requester can approve.", show_alert=True)
        return

    if action == "approve":
        await handle_approve(query, pending, context)
    else:
        await handle_reject(query, pending, context)




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

    # Send working status with animated indicator
    git_info = get_git_info(session.cwd)
    status_message = await update.message.reply_text(
        f"<code>{session.cwd}</code> {git_info}\n\n⏳ Thinking...",
        parse_mode=ParseMode.HTML
    )

    try:
        # Run Claude Code CLI with streaming
        result = await run_claude_streaming(
            prompt=user_message,
            cwd=session.cwd,
            status_message=status_message,
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
            resume_id=session.resume_id
        )

        if result["status"] == "pending_approval":
            # Delete status message
            try:
                await status_message.delete()
            except Exception:
                pass

            # Show approval request
            msg_id = await show_approval_request(
                update.effective_chat.id,
                context,
                result
            )

            # Store pending edit
            pending_edits[result["edit_id"]] = PendingEdit(
                edit_id=result["edit_id"],
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                user_id=user_id,
                tool_name=result["tool_name"],
                file_path=result["file_path"],
                old_string=result["old_string"],
                new_string=result["new_string"],
                process=result["process"],
                session_id=result["session_id"],
                cwd=session.cwd,
            )

        elif result["status"] == "complete":
            # Update session with new resume ID if provided
            if result.get("session_id"):
                sessions.update(user_id, resume_id=result["session_id"])

            # Delete status message
            try:
                await status_message.delete()
            except Exception:
                pass

            # Send response
            response = result.get("response", "")
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

        elif result["status"] == "error":
            # Delete status message
            try:
                await status_message.delete()
            except Exception:
                pass
            await update.message.reply_text(result.get("message", "Unknown error"))

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
    app.add_handler(CallbackQueryHandler(handle_edit_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"Bot running! Using Claude Code CLI directly.")
    print(f"Allowed users: {ALLOWED_USER_IDS or 'All'}")
    print(f"Default directory: {DEFAULT_CWD}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
