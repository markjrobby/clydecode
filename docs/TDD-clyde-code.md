# Technical Design Document: Clyde Code

## Overview

This document provides the complete technical specification for Clyde Code, a Telegram bot that bridges to Claude Code CLI. It's written for engineers to understand and implement the system.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Data Structures](#2-data-structures)
3. [Core Components](#3-core-components)
4. [Feature: Streaming Tool Activity](#4-feature-streaming-tool-activity)
5. [Feature: Approval Workflow](#5-feature-approval-workflow)
6. [Feature: Session Management](#6-feature-session-management)
7. [Error Handling](#7-error-handling)
8. [Testing](#8-testing)

---

## 1. System Architecture

### High-Level Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    User's Server (Raspberry Pi)             │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                      bot.py                           │  │
│  │                                                       │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  │  │
│  │  │  Telegram   │  │   Claude    │  │   Session    │  │  │
│  │  │  Handlers   │  │  Streaming  │  │   Manager    │  │  │
│  │  └─────────────┘  └─────────────┘  └──────────────┘  │  │
│  │         │                │                │          │  │
│  │         └────────────────┼────────────────┘          │  │
│  │                          │                           │  │
│  │                          ▼                           │  │
│  │              ┌─────────────────────┐                 │  │
│  │              │   Claude Code CLI   │                 │  │
│  │              │   (subprocess)      │                 │  │
│  │              └─────────────────────┘                 │  │
│  └───────────────────────────────────────────────────────┘  │
│                             │                               │
└─────────────────────────────│───────────────────────────────┘
                              │
                              │ Telegram Bot API (HTTPS)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        Telegram                             │
│                                                             │
│    User's phone/desktop ←→ Telegram servers ←→ Bot         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Request Flow

```
1. User sends message in Telegram
          │
          ▼
2. Telegram servers forward to bot.py via polling
          │
          ▼
3. bot.py spawns Claude CLI subprocess:
   claude -p "message" --output-format stream-json --verbose
          │
          ▼
4. Claude CLI streams JSON to stdout as it works
          │
          ▼
5. bot.py parses JSON, updates Telegram message with progress
          │
          ▼
6. On Edit/Write tool: pause, show diff, wait for approval
          │
          ▼
7. On approval: continue; on reject: kill process
          │
          ▼
8. Show final response to user
```

### File Structure

```
clyde-code/
├── bot.py              # Main application (all logic here for MVP)
├── requirements.txt    # Python dependencies
├── .env.example        # Configuration template
├── .env                # Actual config (gitignored)
├── .gitignore
├── README.md           # User-facing setup guide
├── CLAUDE.md           # Guidelines for AI assistants
└── docs/
    ├── PRD-clyde-code.md   # Product requirements
    └── TDD-clyde-code.md   # This document
```

---

## 2. Data Structures

### 2.1 Session

Tracks user's working context. Persisted to disk.

```python
@dataclass
class Session:
    id: str                    # Unique session ID (timestamp-based)
    cwd: str                   # Current working directory
    created_at: str            # ISO timestamp
    resume_id: Optional[str]   # Claude session ID for conversation continuity

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
```

**Storage:** JSON file at `~/.clyde-code-sessions.json`

```json
{
  "26012399": {
    "id": "20260102143022",
    "cwd": "/home/user/project",
    "created_at": "2026-01-02T14:30:22",
    "resume_id": "abc-123-def"
  }
}
```

### 2.2 PendingEdit

Tracks an edit waiting for user approval. In-memory only.

```python
@dataclass
class PendingEdit:
    edit_id: str                              # Unique ID (8-char UUID)
    chat_id: int                              # Telegram chat ID
    message_id: int                           # Diff message ID (for editing)
    user_id: int                              # Telegram user ID
    tool_name: str                            # "Edit" or "Write"
    file_path: str                            # Absolute path to file
    old_string: str                           # Original content (empty for Write)
    new_string: str                           # New content
    process: asyncio.subprocess.Process       # Claude CLI subprocess
    session_id: str                           # Claude session ID
    created_at: datetime                      # When proposed
```

**Storage:** In-memory dict

```python
pending_edits: dict[str, PendingEdit] = {}
```

### 2.3 SessionManager

Handles session CRUD with disk persistence.

```python
class SessionManager:
    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.sessions: dict[int, Session] = {}  # Key: Telegram user_id
        self._load()

    def _load(self): ...       # Load from JSON file
    def _save(self): ...       # Save to JSON file
    def get_or_create(self, user_id: int) -> Session: ...
    def update(self, user_id: int, **kwargs): ...
    def reset(self, user_id: int, cwd: str = None) -> Session: ...
```

---

## 3. Core Components

### 3.1 Configuration

Loaded from `.env` file:

```python
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()]
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
SESSIONS_FILE = os.path.expanduser(os.getenv("SESSIONS_FILE", "~/.clyde-code-sessions.json"))
```

### 3.2 Authorization

Simple allowlist check:

```python
def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # Empty list = allow all (not recommended)
    return user_id in ALLOWED_USER_IDS
```

### 3.3 Sensitive Data Redaction

Before sending any output to Telegram:

```python
def redact_sensitive(text: str) -> str:
    """Remove API keys, tokens, passwords from text."""
    # Telegram bot tokens
    text = re.sub(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b', '[REDACTED]', text)
    # AWS keys
    text = re.sub(r'\bAKIA[0-9A-Z]{16}\b', '[REDACTED]', text)
    # GitHub tokens
    text = re.sub(r'\bgh[pous]_[A-Za-z0-9]{36}\b', '[REDACTED]', text)
    # ... etc
    return text
```

### 3.4 Markdown to HTML Conversion

Telegram uses a subset of HTML. Convert Claude's markdown:

```python
def markdown_to_html(text: str) -> str:
    # Preserve code blocks first (they shouldn't be processed)
    # Convert **bold** to <b>bold</b>
    # Convert *italic* to <i>italic</i>
    # Convert `code` to <code>code</code>
    # Convert ```blocks``` to <pre>blocks</pre>
    return text
```

---

## 4. Feature: Streaming Tool Activity

### Purpose

Show users what Claude is doing in real-time while it works.

### User Experience

```
/home/user/project (main)

⏳ Analyzing request...
→ Glob: **/*.py
→ Reading: main.py
→ Reading: utils.py
→ Grep: "database"
```

### Implementation

#### 4.1 Claude CLI Command

```bash
claude -p "user prompt" --output-format stream-json --verbose
```

Flags:
- `-p`: Print mode (non-interactive)
- `--output-format stream-json`: Output JSON objects, one per line
- `--verbose`: Include detailed progress info

#### 4.2 JSON Message Types

| Type | Meaning | Action |
|------|---------|--------|
| `system` | Init message | Extract session_id |
| `assistant` | Claude's response or tool use | Parse content blocks |
| `user` | Tool results | Show "✓ Done" |
| `result` | Final response | Return to user |

#### 4.3 Processing Tool Use

```python
# Inside assistant message
content = data.get("message", {}).get("content", [])
for block in content:
    if block.get("type") == "tool_use":
        tool_name = block.get("name")      # "Read", "Edit", "Bash", etc.
        tool_input = block.get("input")    # Tool-specific params

        # Format for display
        display = format_tool_use(tool_name, tool_input)
        # e.g., "Reading: main.py" or "Grep: database"
```

#### 4.4 format_tool_use()

```python
def format_tool_use(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Read":
        return f"Reading: {tool_input.get('file_path', '?').split('/')[-1]}"
    elif tool_name == "Edit":
        return f"Editing: {tool_input.get('file_path', '?').split('/')[-1]}"
    elif tool_name == "Write":
        return f"Writing: {tool_input.get('file_path', '?').split('/')[-1]}"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "?")[:40]
        return f"Running: {cmd}"
    elif tool_name == "Glob":
        return f"Searching: {tool_input.get('pattern', '?')}"
    elif tool_name == "Grep":
        return f"Grep: {tool_input.get('pattern', '?')}"
    else:
        return tool_name
```

#### 4.5 Heartbeat Animation

While waiting for first output, show rotating status:

```python
async def heartbeat():
    phases = ["⏳ Analyzing...", "⏳ Reading...", "⏳ Processing...", "⏳ Thinking..."]
    i = 0
    while not stop_heartbeat[0]:
        if not tool_uses:  # Only animate before first tool
            await update_status(phases[i % len(phases)], force=True)
        i += 1
        await asyncio.sleep(2)
```

---

## 5. Feature: Approval Workflow

### Purpose

Let users review and approve/reject file changes before they're applied.

### User Experience

#### Flow 1: Approve
```
User: "add logging to main.py"

Bot: 📄 main.py

     🟥 def process():
     🟥     result = compute()

     🟩 import logging
     🟩
     🟩 def process():
     🟩     logging.info("Starting process")
     🟩     result = compute()

     [✅ Approve]  [❌ Reject]

User: taps Approve

Bot: ✅ Applied edit to main.py

     "I've added logging to the process function."
```

#### Flow 2: Reject and Redirect
```
User: "add logging to main.py"

Bot: 📄 main.py
     [diff...]
     [✅ Approve]  [❌ Reject]

User: taps Reject

Bot: ❌ Rejected edit to main.py

     What would you like me to do instead?

User: "use structlog instead of logging"

Bot: [new diff with structlog...]
```

### Implementation

#### 5.1 Detect Edit/Write in Stream

In the JSON processing loop:

```python
if block.get("type") == "tool_use":
    tool_name = block.get("name")
    tool_input = block.get("input", {})

    if tool_name == "Edit":
        return {
            "status": "pending_approval",
            "edit_id": str(uuid.uuid4())[:8],
            "tool_name": "Edit",
            "file_path": tool_input.get("file_path", ""),
            "old_string": tool_input.get("old_string", ""),
            "new_string": tool_input.get("new_string", ""),
            "process": process,
            "session_id": session_id
        }

    elif tool_name == "Write":
        return {
            "status": "pending_approval",
            "edit_id": str(uuid.uuid4())[:8],
            "tool_name": "Write",
            "file_path": tool_input.get("file_path", ""),
            "old_string": "",
            "new_string": tool_input.get("content", ""),
            "process": process,
            "session_id": session_id
        }
```

#### 5.2 format_diff()

```python
def format_diff(old_string: str, new_string: str, file_path: str) -> str:
    """Format diff with colored emoji indicators."""
    filename = file_path.split('/')[-1]

    # Truncate if needed
    max_len = 500
    old_display = old_string[:max_len] + "..." if len(old_string) > max_len else old_string
    new_display = new_string[:max_len] + "..." if len(new_string) > max_len else new_string

    # Format lines with emoji
    old_lines = old_display.split('\n')
    old_formatted = '\n'.join(f"🟥 {line}" for line in old_lines)

    new_lines = new_display.split('\n')
    new_formatted = '\n'.join(f"🟩 {line}" for line in new_lines)

    diff_text = f"<b>📄 {filename}</b>\n\n"
    diff_text += f"<pre>{html.escape(old_formatted)}</pre>\n\n"
    diff_text += f"<pre>{html.escape(new_formatted)}</pre>"

    return diff_text
```

#### 5.3 format_new_file()

```python
def format_new_file(content: str, file_path: str) -> str:
    """Format new file preview (all green)."""
    filename = file_path.split('/')[-1]

    max_len = 500
    display = content[:max_len] + "..." if len(content) > max_len else content

    lines = display.split('\n')
    formatted = '\n'.join(f"🟩 {line}" for line in lines)

    text = f"<b>📄 {filename}</b> (new file)\n\n"
    text += f"<pre>{html.escape(formatted)}</pre>"

    return text
```

#### 5.4 Show Approval Buttons

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def show_approval_request(update, context, edit_info, status_message):
    edit_id = edit_info["edit_id"]

    # Delete working status
    try:
        await status_message.delete()
    except:
        pass

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
        chat_id=update.effective_chat.id,
        text=diff_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    # Store pending edit
    pending_edits[edit_id] = PendingEdit(
        edit_id=edit_id,
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        user_id=update.effective_user.id,
        tool_name=edit_info["tool_name"],
        file_path=edit_info["file_path"],
        old_string=edit_info["old_string"],
        new_string=edit_info["new_string"],
        process=edit_info["process"],
        session_id=edit_info["session_id"],
        created_at=datetime.now()
    )
```

#### 5.5 Handle Button Callbacks

```python
from telegram.ext import CallbackQueryHandler

async def handle_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# Register in main():
app.add_handler(CallbackQueryHandler(handle_edit_callback))
```

#### 5.6 handle_approve()

```python
async def handle_approve(query, pending: PendingEdit, context):
    filename = pending.file_path.split('/')[-1]

    await query.edit_message_text(
        f"✅ Approved edit to <b>{filename}</b>\n\nContinuing...",
        parse_mode=ParseMode.HTML
    )

    # Continue reading Claude output
    full_response = ""
    try:
        while True:
            chunk = await asyncio.wait_for(
                pending.process.stdout.read(4096),
                timeout=300
            )
            if not chunk:
                break

            for line in chunk.decode().split('\n'):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "result":
                        full_response = data.get("result", "")
                except json.JSONDecodeError:
                    continue

        await pending.process.wait()

        if full_response:
            response_html = markdown_to_html(redact_sensitive(full_response))
            await context.bot.send_message(
                chat_id=pending.chat_id,
                text=truncate_message(response_html),
                parse_mode=ParseMode.HTML
            )

    except Exception as e:
        await context.bot.send_message(
            chat_id=pending.chat_id,
            text=f"Error: {str(e)[:200]}"
        )
```

#### 5.7 handle_reject()

```python
async def handle_reject(query, pending: PendingEdit, context):
    filename = pending.file_path.split('/')[-1]

    # Kill Claude process
    try:
        pending.process.kill()
        await pending.process.wait()
    except:
        pass

    # Prompt for new instructions
    await query.edit_message_text(
        f"❌ Rejected edit to <b>{filename}</b>\n\n"
        f"What would you like me to do instead?",
        parse_mode=ParseMode.HTML
    )
```

---

## 6. Feature: Session Management

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize bot, show status |
| `/new [path]` | Start new session in directory |
| `/cwd` | Show current working directory |
| `/cwd [path]` | Change working directory |
| `/status` | Show bot and session status |
| `/git [args]` | Run git command |
| `/help` | Show available commands |

### Implementation

Each command is a handler function:

```python
from telegram.ext import CommandHandler

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    session = sessions.get_or_create(update.effective_user.id)
    git_info = get_git_info(session.cwd)

    await update.message.reply_text(
        f"Clyde Code ready.\n\n"
        f"Directory: <code>{session.cwd}</code> {git_info}\n\n"
        f"Send any message to chat with Claude.",
        parse_mode=ParseMode.HTML
    )

# Register in main():
app.add_handler(CommandHandler("start", cmd_start))
```

### Git Info Helper

```python
def get_git_info(cwd: str) -> str:
    """Get branch and status for display."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if branch.returncode != 0:
            return ""

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        changes = len([l for l in status.stdout.strip().split("\n") if l])

        if changes > 0:
            return f"({branch.stdout.strip()}, {changes} uncommitted)"
        return f"({branch.stdout.strip()})"
    except:
        return ""
```

---

## 7. Error Handling

### 7.1 Claude CLI Errors

```python
if process.returncode != 0:
    stderr = await process.stderr.read()
    error_msg = stderr.decode().strip()[:500]
    logger.error(f"Claude CLI error: {error_msg}")
    return {"status": "error", "message": error_msg}
```

### 7.2 Telegram API Errors

```python
try:
    await status_message.edit_text(...)
except Exception:
    pass  # Message may have been deleted
```

### 7.3 Stale Edit Cleanup

```python
async def cleanup_stale_edits():
    """Kill edits pending for more than 10 minutes."""
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        stale = [
            eid for eid, edit in pending_edits.items()
            if (now - edit.created_at).total_seconds() > 600
        ]
        for edit_id in stale:
            edit = pending_edits.pop(edit_id)
            try:
                edit.process.kill()
            except:
                pass
```

### 7.4 Timeout Handling

```python
try:
    chunk = await asyncio.wait_for(
        process.stdout.read(4096),
        timeout=300  # 5 minutes
    )
except asyncio.TimeoutError:
    process.kill()
    return {"status": "error", "message": "Request timed out"}
```

---

## 8. Testing

### 8.1 Unit Tests

| Test | Description |
|------|-------------|
| `test_format_diff` | Diff formatting with emoji |
| `test_format_new_file` | New file formatting |
| `test_redact_sensitive` | API keys removed |
| `test_markdown_to_html` | Conversion works |
| `test_is_authorized` | Allowlist works |

### 8.2 Integration Tests

| Test | Description |
|------|-------------|
| Simple message | Send "hello", get response |
| Edit approval | Trigger edit, approve, verify |
| Edit rejection | Trigger edit, reject, send new instruction |
| New file | Trigger write, approve, verify file exists |
| Session persistence | Restart bot, session preserved |

### 8.3 Manual Testing Checklist

- [ ] `/start` shows status
- [ ] `/cwd` shows and changes directory
- [ ] `/git status` works
- [ ] Simple question gets answer
- [ ] Edit shows diff with 🟥/🟩
- [ ] Approve button continues
- [ ] Reject button prompts for alternative
- [ ] New file shows with Create/Cancel
- [ ] Long diff truncates properly
- [ ] Secrets are redacted

---

## Appendix: Full run_claude_streaming() Signature

```python
async def run_claude_streaming(
    prompt: str,
    cwd: str,
    status_message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    resume_id: str = None
) -> dict:
    """
    Run Claude CLI and stream output.

    Args:
        prompt: User's message
        cwd: Working directory for Claude
        status_message: Telegram message to update with progress
        context: Telegram bot context
        chat_id: Telegram chat ID
        resume_id: Claude session ID for conversation continuity

    Returns:
        dict with one of:
        - {"status": "complete", "response": str, "session_id": str}
        - {"status": "pending_approval", "edit_id": str, "tool_name": str, ...}
        - {"status": "error", "message": str}
    """
```

---

## Appendix: Dependencies

```
# requirements.txt
python-telegram-bot>=21.0
python-dotenv>=1.0.0
```

No additional dependencies needed for MVP.
