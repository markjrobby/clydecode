# Technical Design Document: Approval Workflow

## Purpose

This document explains how to implement the approval workflow for Clyde Code. After reading this, you should understand exactly how the bot pauses when Claude wants to edit files, shows a diff to the user, and resumes or aborts based on their response.

---

## Background

### What happens today

1. User sends a message to the Telegram bot
2. Bot runs Claude CLI: `claude -p "user message" --output-format stream-json --verbose`
3. Claude streams JSON output as it works (reading files, thinking, editing)
4. Bot parses the JSON and shows tool activity in real-time
5. When Claude finishes, bot sends the final response

### The problem

Claude edits files automatically. The user has no chance to review or reject changes before they're applied.

### What we're building

Insert a "pause" step when Claude wants to edit a file:

1. User sends message
2. Claude works, streams output
3. **Claude wants to edit a file** ← Pause here
4. **Bot shows diff with Approve/Reject buttons**
5. **User taps Approve or Reject**
6. **If Approve:** Bot lets Claude continue
7. **If Reject:** Bot stops Claude, user can give new instructions
8. Claude continues or user starts new request
9. Bot sends final response

---

## User Flows

### Flow 1: User Approves

```
User: "add retry logic to api.py"
              ↓
Bot: ⏳ Analyzing request...
     → Reading: api.py
     → Editing: api.py
              ↓
Bot: 📄 api.py

     🟥 response = requests.get(url)

     🟩 try:
     🟩     response = requests.get(url)
     🟩 except RequestException:
     🟩     sleep(1)
     🟩     response = requests.get(url)

     [✅ Approve]  [❌ Reject]
              ↓
User: taps [✅ Approve]
              ↓
Bot: ✅ Applied edit to api.py

     "I've added basic retry logic that waits 1 second
      before retrying failed requests."
```

### Flow 2: User Rejects and Redirects

```
User: "add retry logic to api.py"
              ↓
Bot: 📄 api.py

     🟥 response = requests.get(url)

     🟩 try:
     🟩     response = requests.get(url)
     🟩 except:
     🟩     response = requests.get(url)

     [✅ Approve]  [❌ Reject]
              ↓
User: taps [❌ Reject]
              ↓
Bot: ❌ Edit rejected.

     What would you like me to do instead?
              ↓
User: "use exponential backoff instead, max 3 retries"
              ↓
Bot: ⏳ Analyzing request...
     → Reading: api.py
     → Editing: api.py
              ↓
Bot: 📄 api.py

     🟥 response = requests.get(url)

     🟩 for attempt in range(3):
     🟩     try:
     🟩         response = requests.get(url)
     🟩         break
     🟩     except RequestException:
     🟩         if attempt == 2:
     🟩             raise
     🟩         sleep(2 ** attempt)

     [✅ Approve]  [❌ Reject]
              ↓
User: taps [✅ Approve]
              ↓
Bot: ✅ Applied edit to api.py
```

### Flow 3: User Rejects and Cancels

```
User: "refactor the entire auth module"
              ↓
Bot: 📄 auth.py

     🟥 [lots of code]

     🟩 [lots of different code]

     [✅ Approve]  [❌ Reject]
              ↓
User: taps [❌ Reject]
              ↓
Bot: ❌ Edit rejected.

     What would you like me to do instead?
              ↓
User: "actually never mind, let's leave it as is"
              ↓
Bot: "No problem! Let me know if you need anything else."
```

---

## Key Concepts

### Claude CLI Output Format

When you run Claude with `--output-format stream-json`, it outputs one JSON object per line:

#### 1. System Init
```json
{"type": "system", "subtype": "init", "session_id": "abc-123", ...}
```

#### 2. Assistant Message with Tool Use
```json
{
  "type": "assistant",
  "message": {
    "content": [
      {
        "type": "tool_use",
        "id": "toolu_01ABC123",
        "name": "Edit",
        "input": {
          "file_path": "/home/user/project/main.py",
          "old_string": "def hello():\n    print('hi')",
          "new_string": "def hello():\n    print('hello world')"
        }
      }
    ]
  }
}
```
**This is what we intercept.** When `name` is "Edit" or "Write", we pause.

#### 3. Tool Result
```json
{
  "type": "user",
  "message": {
    "content": [{"type": "tool_result", "tool_use_id": "toolu_01ABC123", ...}]
  }
}
```

#### 4. Final Result
```json
{
  "type": "result",
  "result": "I've updated the hello function.",
  "session_id": "abc-123"
}
```

### Telegram Inline Buttons

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

keyboard = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Approve", callback_data="approve_abc123"),
        InlineKeyboardButton("❌ Reject", callback_data="reject_abc123")
    ]
])

await context.bot.send_message(
    chat_id=chat_id,
    text="Do you approve?",
    reply_markup=keyboard
)
```

When user taps a button, bot receives a `CallbackQuery` with the `callback_data`.

---

## Architecture

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                         START                               │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              User sends message to bot                       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│         Bot spawns Claude CLI subprocess                     │
│         claude -p "message" --output-format stream-json      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              Bot reads JSON stream                           │
│              Shows tool activity (→ Reading, → Grep, etc)    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  Edit/Write     │
                    │  tool detected? │
                    └─────────────────┘
                       │           │
                      YES          NO
                       │           │
                       ▼           ▼
        ┌──────────────────┐    ┌──────────────────────────┐
        │ Pause processing │    │ Continue until "result"  │
        │ Store process    │    │ Show final response      │
        │ Show diff        │    │ END                      │
        │ Show buttons     │    └──────────────────────────┘
        └──────────────────┘
                 │
                 ▼
        ┌─────────────────┐
        │ Wait for user   │
        │ button tap      │
        └─────────────────┘
                 │
        ┌────────┴────────┐
        │                 │
     APPROVE           REJECT
        │                 │
        ▼                 ▼
┌───────────────┐  ┌────────────────────┐
│ Continue      │  │ Kill process       │
│ reading       │  │ Send "rejected"    │
│ stream        │  │ message            │
└───────────────┘  └────────────────────┘
        │                 │
        ▼                 ▼
┌───────────────┐  ┌────────────────────┐
│ Show final    │  │ Wait for user's    │
│ response      │  │ next message       │
│ END           │  │ (new instructions) │
└───────────────┘  └────────────────────┘
                          │
                          ▼
                   ┌─────────────────┐
                   │ User sends new  │
                   │ message? START  │
                   │ over from top   │
                   └─────────────────┘
```

---

## Data Structures

### PendingEdit

Stores information about an edit waiting for approval:

```python
from dataclasses import dataclass
from datetime import datetime
import asyncio

@dataclass
class PendingEdit:
    edit_id: str                              # Unique ID (e.g., "a1b2c3d4")
    chat_id: int                              # Telegram chat ID
    message_id: int                           # The diff message ID (to edit later)
    user_id: int                              # User who started the task
    tool_name: str                            # "Edit" or "Write"
    file_path: str                            # Full path to file
    old_string: str                           # Original content (empty for Write)
    new_string: str                           # New content
    process: asyncio.subprocess.Process       # Claude CLI subprocess
    session_id: str                           # Claude session ID (for resume)
    created_at: datetime                      # When edit was proposed
```

### Global Storage

```python
# Key: edit_id, Value: PendingEdit
pending_edits: dict[str, PendingEdit] = {}
```

---

## Implementation Steps

### Step 1: Add imports and data structures

At the top of `bot.py`, add:

```python
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
```

After the `Session` class, add:

```python
@dataclass
class PendingEdit:
    """An edit waiting for user approval."""
    edit_id: str
    chat_id: int
    message_id: int
    user_id: int
    tool_name: str
    file_path: str
    old_string: str
    new_string: str
    process: asyncio.subprocess.Process
    session_id: str
    created_at: datetime

# Global storage
pending_edits: dict[str, PendingEdit] = {}
```

### Step 2: Modify run_claude_streaming() return type

Currently the function returns `(response_text, session_id)`. Change it to return a dict that can indicate different states:

```python
async def run_claude_streaming(...) -> dict:
    """
    Returns dict with:
    - {"status": "complete", "response": "...", "session_id": "..."}
    - {"status": "pending_approval", "edit_id": "...", "tool_name": "...", ...}
    - {"status": "error", "message": "..."}
    """
```

### Step 3: Detect Edit/Write and pause

Inside the JSON processing loop, when we see an Edit or Write tool_use:

```python
# In the loop processing assistant messages...

if block.get("type") == "tool_use":
    tool_name = block.get("name", "")
    tool_input = block.get("input", {})

    # Is this an Edit or Write?
    if tool_name == "Edit":
        edit_id = str(uuid.uuid4())[:8]
        return {
            "status": "pending_approval",
            "edit_id": edit_id,
            "tool_name": "Edit",
            "file_path": tool_input.get("file_path", ""),
            "old_string": tool_input.get("old_string", ""),
            "new_string": tool_input.get("new_string", ""),
            "process": process,
            "session_id": session_id
        }

    elif tool_name == "Write":
        edit_id = str(uuid.uuid4())[:8]
        return {
            "status": "pending_approval",
            "edit_id": edit_id,
            "tool_name": "Write",
            "file_path": tool_input.get("file_path", ""),
            "old_string": "",  # No old content for new files
            "new_string": tool_input.get("content", ""),
            "process": process,
            "session_id": session_id
        }

    # Other tools: display and continue
    tool_display = format_tool_use(tool_name, tool_input)
    tool_uses.append(tool_display)
    await update_status(...)
```

### Step 4: Update handle_message() to handle pending edits

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... existing setup code ...

    result = await run_claude_streaming(
        prompt=user_message,
        cwd=session.cwd,
        status_message=status_message,
        context=context,
        chat_id=update.effective_chat.id,
        resume_id=session.resume_id
    )

    # Handle different result types
    if result["status"] == "pending_approval":
        await show_approval_request(update, context, result, status_message)
        return

    elif result["status"] == "error":
        await status_message.edit_text(f"Error: {result['message']}")
        return

    elif result["status"] == "complete":
        # ... existing code to show response ...
        response = result["response"]
        session_id = result["session_id"]
        # ... rest of existing code ...
```

### Step 5: Create show_approval_request()

```python
async def show_approval_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit_info: dict,
    status_message
):
    """Display the diff and approval buttons."""

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    edit_id = edit_info["edit_id"]

    # Delete the "working" status message
    try:
        await status_message.delete()
    except Exception:
        pass

    # Format the diff
    if edit_info["tool_name"] == "Edit":
        diff_text = format_diff(
            edit_info["old_string"],
            edit_info["new_string"],
            edit_info["file_path"]
        )
        button_approve = "✅ Approve"
        button_reject = "❌ Reject"
    else:  # Write
        diff_text = format_new_file(
            edit_info["new_string"],
            edit_info["file_path"]
        )
        button_approve = "✅ Create"
        button_reject = "❌ Cancel"

    # Create buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(button_approve, callback_data=f"approve_{edit_id}"),
            InlineKeyboardButton(button_reject, callback_data=f"reject_{edit_id}")
        ]
    ])

    # Send the diff message with buttons
    diff_message = await context.bot.send_message(
        chat_id=chat_id,
        text=diff_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    # Store the pending edit
    pending_edits[edit_id] = PendingEdit(
        edit_id=edit_id,
        chat_id=chat_id,
        message_id=diff_message.message_id,
        user_id=user_id,
        tool_name=edit_info["tool_name"],
        file_path=edit_info["file_path"],
        old_string=edit_info["old_string"],
        new_string=edit_info["new_string"],
        process=edit_info["process"],
        session_id=edit_info["session_id"],
        created_at=datetime.now()
    )
```

### Step 6: Create format_new_file() helper

```python
def format_new_file(content: str, file_path: str) -> str:
    """Format a new file preview."""
    filename = file_path.split('/')[-1]

    # Truncate if too long
    max_len = 500
    display = content[:max_len] + "..." if len(content) > max_len else content

    # All lines are new (green)
    lines = display.split('\n')
    formatted = '\n'.join(f"🟩 {line}" for line in lines)

    text = f"<b>📄 {filename}</b> (new file)\n\n"
    text += f"<pre>{html.escape(formatted)}</pre>"

    return text
```

### Step 7: Create the callback handler

```python
async def handle_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Approve/Reject button taps."""

    query = update.callback_query
    await query.answer()  # Acknowledge the tap (removes loading spinner)

    # Parse callback data: "approve_abc123" or "reject_abc123"
    data = query.data
    if not data or "_" not in data:
        return

    action, edit_id = data.split("_", 1)

    # Find the pending edit
    if edit_id not in pending_edits:
        await query.edit_message_text(
            "⚠️ This edit has expired or was already processed."
        )
        return

    pending = pending_edits.pop(edit_id)

    # Verify user is the one who initiated
    if query.from_user.id != pending.user_id:
        pending_edits[edit_id] = pending  # Put it back
        await query.answer("Only the user who started this task can approve/reject.", show_alert=True)
        return

    if action == "approve":
        await handle_approve(query, pending, context)
    elif action == "reject":
        await handle_reject(query, pending, context)
```

### Step 8: Implement handle_approve()

```python
async def handle_approve(
    query,
    pending: PendingEdit,
    context: ContextTypes.DEFAULT_TYPE
):
    """User approved. Continue Claude and show result."""

    filename = pending.file_path.split('/')[-1]

    # Update message to show approval
    await query.edit_message_text(
        f"✅ Approved edit to <b>{filename}</b>\n\nContinuing...",
        parse_mode=ParseMode.HTML
    )

    # Continue reading from Claude process until it finishes
    try:
        full_response = ""
        session_id = pending.session_id

        # Read remaining output
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

            # Process the JSON lines
            # (simplified - in real impl, use buffer like in run_claude_streaming)
            for line in chunk.decode().split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "result":
                        full_response = data.get("result", "")
                        session_id = data.get("session_id", session_id)
                except json.JSONDecodeError:
                    continue

        await pending.process.wait()

        # Update session with new session_id for conversation continuity
        sessions.update(pending.user_id, resume_id=session_id)

        # Send the final response
        if full_response:
            response_html = markdown_to_html(redact_sensitive(full_response))
            response_html = truncate_message(response_html)
            await context.bot.send_message(
                chat_id=pending.chat_id,
                text=response_html,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=pending.chat_id,
                text=f"✅ Edit applied to {filename}."
            )

    except Exception as e:
        logger.error(f"Error continuing after approval: {e}")
        await context.bot.send_message(
            chat_id=pending.chat_id,
            text=f"Error: {str(e)[:200]}"
        )
```

### Step 9: Implement handle_reject()

```python
async def handle_reject(
    query,
    pending: PendingEdit,
    context: ContextTypes.DEFAULT_TYPE
):
    """User rejected. Kill Claude and ask for new instructions."""

    filename = pending.file_path.split('/')[-1]

    # Kill the Claude process
    try:
        pending.process.kill()
        await pending.process.wait()
    except Exception:
        pass

    # Update message to show rejection and prompt for next action
    await query.edit_message_text(
        f"❌ Rejected edit to <b>{filename}</b>\n\n"
        f"What would you like me to do instead?",
        parse_mode=ParseMode.HTML
    )

    # Note: The user's next message will start a fresh Claude session.
    # The conversation context is maintained via session.resume_id,
    # so Claude will remember what was being worked on.
```

### Step 10: Register the callback handler

In `main()`, add the callback handler:

```python
def main():
    # ... existing code ...

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    # ... other handlers ...

    # Add callback handler for approve/reject buttons
    app.add_handler(CallbackQueryHandler(handle_edit_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ... rest of existing code ...
```

---

## Important Notes

### Claude CLI Behavior

When Claude outputs a tool_use for Edit in stream-json mode, the edit has already been queued but may or may not have been executed yet. This is a limitation of using the CLI.

For MVP, we accept this:
- The edit likely happens before user can reject
- Rejection stops further edits
- User can use `/git checkout <file>` to revert

Future improvement: Use Claude API directly instead of CLI for full control.

### Session Continuity

After rejection, the user can send new instructions. These go to a NEW Claude process, but with the same `--resume` session ID, so Claude remembers the context.

Example:
```
User: "add logging"
Claude: [proposes edit]
User: [rejects]
Bot: "What would you like me to do instead?"
User: "use the logging module instead of print"
Claude: [remembers the context, proposes new edit]
```

### Timeouts

Consider adding a cleanup task for edits that are never approved/rejected:

```python
async def cleanup_stale_edits():
    """Kill edits pending for more than 10 minutes."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        now = datetime.now()
        stale = [
            edit_id for edit_id, edit in pending_edits.items()
            if (now - edit.created_at).total_seconds() > 600
        ]
        for edit_id in stale:
            edit = pending_edits.pop(edit_id)
            try:
                edit.process.kill()
            except:
                pass
            try:
                await context.bot.edit_message_text(
                    chat_id=edit.chat_id,
                    message_id=edit.message_id,
                    text="⚠️ This edit request has expired."
                )
            except:
                pass
```

Start this in `main()`:
```python
asyncio.create_task(cleanup_stale_edits())
```

---

## Testing Checklist

### Basic Flow
- [ ] Send message that triggers an edit
- [ ] Diff appears with 🟥/🟩 formatting
- [ ] Approve/Reject buttons appear
- [ ] Tapping Approve continues and shows response
- [ ] Tapping Reject shows "What would you like me to do instead?"

### Edge Cases
- [ ] Multiple edits in sequence (one at a time)
- [ ] New file creation (Write tool)
- [ ] Very long diffs (truncation works)
- [ ] User sends message while edit is pending (should work, new session)
- [ ] Edit expires after 10 minutes (cleanup works)

### Error Cases
- [ ] Claude process crashes during approval wait
- [ ] Network error sending message
- [ ] Invalid callback data

---

## Files Changed

| File | Changes |
|------|---------|
| `bot.py` | Add PendingEdit, modify run_claude_streaming, add callback handlers |

## New Dependencies

None - uses existing `python-telegram-bot` library.

---

## Summary

1. **Detect** Edit/Write tool_use in Claude's stream output
2. **Pause** by returning early from run_claude_streaming with edit info
3. **Show** diff with 🟥/🟩 emojis and Approve/Reject buttons
4. **Wait** for user to tap a button
5. **Approve**: Continue reading Claude output, show final response
6. **Reject**: Kill process, prompt user for new instructions
