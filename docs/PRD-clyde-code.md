# PRD: Clyde Code (MVP)

## Overview

Clyde Code is a Telegram bot that brings Claude Code CLI to mobile. Run coding tasks from anywhere via Telegram with a diff review and approval workflow.

## Goal

A working Telegram bot that:
1. Chats with Claude Code CLI
2. Shows diffs with visual distinction (🟥/🟩 emoji indicators)
3. Lets you approve/reject changes before they're applied

---

## User Flow

```
User: "add retry logic to the API client"
              ↓
Bot: ⏳ Analyzing request...
     → Reading: api_client.py
     → Editing: api_client.py
              ↓
Bot: 📄 api_client.py

     🟥 response = requests.get(url)

     🟩 for attempt in range(3):
     🟩     try:
     🟩         response = requests.get(url)
     🟩         break
     🟩     except RequestException:
     🟩         if attempt == 2:
     🟩             raise
     🟩         sleep(1)

     [✅ Approve]  [❌ Reject]
              ↓
User: taps Approve
              ↓
Bot: ✅ Applied. "I've added retry logic with 3 attempts..."
```

---

## Features

### 1. Streaming Tool Activity (Done)
Show what Claude is doing in real-time.

### 2. Inline Diff Preview (To Build)
When Claude wants to edit a file:
- Show diff with emoji indicators:
  - 🟥 for removed lines (red square)
  - 🟩 for added lines (green square)
- Approve/Reject inline buttons
- Code in `<pre>` blocks for monospace + copy/paste

### 3. Approval Workflow (To Build)
- Bot pauses when Claude wants to edit/write
- Waits for user approval
- Resumes or aborts based on response

### 4. New File Preview (To Build)
When Claude creates new files:
- Show preview with 🟩 indicators (all new)
- Create/Cancel buttons

---

## Design Decision: No Mini App

**Why no Mini App for MVP:**
- Requires external HTTPS hosting
- Code diffs would be exposed in URL parameters
- Adds infrastructure complexity

**Inline approach benefits:**
- Zero external dependencies
- Code stays within Telegram's encrypted chat
- Copy/paste works natively
- 🟥/🟩 emojis provide visual distinction

**Future consideration:**
Mini App could be added later for enhanced syntax highlighting if needed, with proper security (encrypted payloads, self-hosted).

---

## Technical Architecture

```
┌─────────────────────────────────────────┐
│            Raspberry Pi                 │
│                                         │
│  bot.py                                 │
│    ├─ Telegram bot                      │
│    ├─ Spawns Claude CLI                 │
│    ├─ Parses stream output              │
│    ├─ Detects Edit/Write tool use       │
│    ├─ Formats diff with emoji           │
│    ├─ Sends approve/reject buttons      │
│    └─ Handles callbacks                 │
│                                         │
└─────────────────────────────────────────┘
              │
              │ Telegram API (encrypted)
              ▼
┌─────────────────────────────────────────┐
│            Telegram                     │
│                                         │
│  Private chat with bot                  │
│  - Inline diff previews                 │
│  - Approve/Reject buttons               │
│                                         │
└─────────────────────────────────────────┘
```

---

## Diff Formatting Spec

### Edit (file modification)
```
📄 filename.py

🟥 old line 1
🟥 old line 2

🟩 new line 1
🟩 new line 2
🟩 new line 3

[✅ Approve]  [❌ Reject]
```

### Write (new file)
```
📄 new_file.py (new)

🟩 line 1
🟩 line 2
🟩 line 3
...

[✅ Create]  [❌ Cancel]
```

### Truncation
- Max 500 chars per section (old/new)
- Add "..." if truncated
- Show line count: `(+12 -3 lines)`

---

## Implementation Tasks

### Phase 1: Approval Workflow
- [ ] Detect Edit tool_use in Claude's stream output
- [ ] Pause/buffer Claude process on edit detection
- [ ] Format diff with 🟥/🟩 emoji indicators
- [ ] Send message with inline Approve/Reject buttons
- [ ] Handle button callback
- [ ] Resume Claude on approve
- [ ] Kill process and notify on reject

### Phase 2: Write Support
- [ ] Detect Write tool_use
- [ ] Show new file preview with 🟩 indicators
- [ ] Create/Cancel buttons
- [ ] Same approve/reject flow

### Phase 3: Polish
- [ ] Show line counts (+X -Y)
- [ ] Better truncation with context
- [ ] Handle multiple edits in sequence
- [ ] Timeout handling
- [ ] Error recovery

---

## File Structure

```
clyde-code/
├── bot.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── CLAUDE.md
└── docs/
    └── PRD-clyde-code.md
```

---

## Security

1. **Code stays in Telegram** - No external services see your code
2. **ALLOWED_USER_IDS** - Only authorized users can interact
3. **Secrets redacted** - API keys, tokens filtered from output
4. **Public repo safe** - No secrets in code, only .env.example with placeholders

---

## Open Questions

1. **Timeout** - If user doesn't approve within X minutes, what happens?
2. **Multiple edits** - Show all at once or one by one?
3. **Bash approval** - Should dangerous commands (rm, sudo) also require approval?
