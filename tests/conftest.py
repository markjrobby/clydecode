"""
Pytest fixtures and configuration for Clyde Code tests.
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import bot


@pytest.fixture
def temp_sessions_file():
    """Create a temporary sessions file."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('{}')
        temp_path = f.name
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def temp_cwd():
    """Create a temporary working directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def session_manager(temp_sessions_file):
    """Create a SessionManager with a temporary file."""
    return bot.SessionManager(temp_sessions_file)


@pytest.fixture
def sample_session():
    """Create a sample Session object."""
    return bot.Session(
        id="20260102143022",
        cwd="/home/user/project",
        created_at="2026-01-02T14:30:22",
        resume_id="abc-123-def"
    )


@pytest.fixture
def sample_pending_edit():
    """Create a sample PendingEdit object."""
    mock_process = MagicMock()
    mock_process.kill = MagicMock()
    mock_process.wait = AsyncMock()
    mock_process.stdout = MagicMock()
    mock_process.stdout.read = AsyncMock(return_value=b'')

    return bot.PendingEdit(
        edit_id="abc12345",
        chat_id=12345,
        message_id=67890,
        user_id=111222,
        tool_name="Edit",
        file_path="/home/user/project/main.py",
        old_string="def old():\n    pass",
        new_string="def new():\n    return True",
        process=mock_process,
        session_id="session-123",
        cwd="/home/user/project",
        created_at=datetime.now()
    )


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat = MagicMock()
    update.effective_chat.id = 12345
    update.message = MagicMock()
    update.message.text = "test message"
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock Telegram context object."""
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_chat_action = AsyncMock()
    context.args = []
    return context


@pytest.fixture
def mock_callback_query():
    """Create a mock callback query for button clicks."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = 111222
    query.data = "approve_abc12345"
    return query


@pytest.fixture
def sample_claude_stream_output():
    """Sample Claude CLI stream-json output."""
    return [
        '{"type": "system", "session_id": "sess-123"}',
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "I will help you."}]}}',
        '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/test/file.py"}}]}}',
        '{"type": "user", "message": {"content": [{"type": "tool_result", "content": "file contents"}]}}',
        '{"type": "result", "result": "Done!", "session_id": "sess-123"}'
    ]


@pytest.fixture
def sample_edit_stream_output():
    """Sample Claude CLI output that triggers an Edit."""
    return [
        '{"type": "system", "session_id": "sess-123"}',
        '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/test/file.py", "old_string": "old code", "new_string": "new code"}}]}}'
    ]


@pytest.fixture
def sample_write_stream_output():
    """Sample Claude CLI output that triggers a Write."""
    return [
        '{"type": "system", "session_id": "sess-123"}',
        '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "/test/new_file.py", "content": "print(\"hello\")"}}]}}'
    ]


# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
