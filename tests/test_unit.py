"""
Unit tests for Clyde Code bot.

Tests individual functions in isolation with mocked dependencies.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot


class TestRedactSensitive:
    """Tests for redact_sensitive function."""

    def test_redacts_telegram_token(self):
        text = "Token: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789"
        result = bot.redact_sensitive(text)
        assert "[TELEGRAM_TOKEN_REDACTED]" in result
        assert "1234567890:" not in result

    def test_redacts_aws_key(self):
        text = "AWS Key: AKIAIOSFODNN7EXAMPLE"
        result = bot.redact_sensitive(text)
        assert "[AWS_KEY_REDACTED]" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_redacts_github_token(self):
        text = "GH Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        result = bot.redact_sensitive(text)
        assert "[GITHUB_TOKEN_REDACTED]" in result
        assert "ghp_" not in result

    def test_redacts_anthropic_key(self):
        text = "API Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
        result = bot.redact_sensitive(text)
        assert "[ANTHROPIC_KEY_REDACTED]" in result
        assert "sk-ant-" not in result

    def test_redacts_openai_key(self):
        # OpenAI keys are 51 chars after sk- prefix (total 54 chars)
        text = "OpenAI: sk-1234567890abcdefghijklmnopqrstuvwxyz123456789012"
        result = bot.redact_sensitive(text)
        assert "[OPENAI_KEY_REDACTED]" in result

    def test_redacts_jwt_token(self):
        text = "JWT: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = bot.redact_sensitive(text)
        assert "[JWT_REDACTED]" in result

    def test_redacts_env_style_secrets(self):
        text = 'API_KEY="supersecretkey123456789"'
        result = bot.redact_sensitive(text)
        assert "[REDACTED]" in result
        assert "supersecretkey123456789" not in result

    def test_redacts_database_passwords(self):
        text = "postgres://user:secretpassword@localhost/db"
        result = bot.redact_sensitive(text)
        assert "[REDACTED]" in result
        assert "secretpassword" not in result

    def test_preserves_normal_text(self):
        text = "This is a normal message without secrets."
        result = bot.redact_sensitive(text)
        assert result == text

    def test_handles_multiple_secrets(self):
        text = "Token: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789 and AKIAIOSFODNN7EXAMPLE"
        result = bot.redact_sensitive(text)
        assert "[TELEGRAM_TOKEN_REDACTED]" in result
        assert "[AWS_KEY_REDACTED]" in result


class TestMarkdownToHtml:
    """Tests for markdown_to_html function."""

    def test_converts_bold(self):
        result = bot.markdown_to_html("This is **bold** text")
        assert "<b>bold</b>" in result

    def test_converts_italic(self):
        result = bot.markdown_to_html("This is *italic* text")
        assert "<i>italic</i>" in result

    def test_converts_strikethrough(self):
        result = bot.markdown_to_html("This is ~~strikethrough~~ text")
        assert "<s>strikethrough</s>" in result

    def test_converts_inline_code(self):
        result = bot.markdown_to_html("Use `code` here")
        assert "<code>code</code>" in result

    def test_converts_code_blocks(self):
        result = bot.markdown_to_html("```python\nprint('hello')\n```")
        assert "<pre>" in result
        assert "print(&#x27;hello&#x27;)" in result

    def test_escapes_html_entities(self):
        result = bot.markdown_to_html("Use <script> tag & more")
        assert "&lt;script&gt;" in result
        assert "&amp;" in result

    def test_preserves_code_content(self):
        result = bot.markdown_to_html("Run `<script>alert('xss')</script>`")
        assert "<code>" in result
        assert "&lt;script&gt;" in result


class TestTruncateMessage:
    """Tests for truncate_message function."""

    def test_short_message_unchanged(self):
        text = "Short message"
        result = bot.truncate_message(text)
        assert result == text

    def test_long_message_truncated(self):
        text = "a" * 5000
        result = bot.truncate_message(text)
        assert len(result) <= 4000
        assert "[Truncated]" in result

    def test_custom_max_length(self):
        text = "a" * 200
        result = bot.truncate_message(text, max_length=100)
        assert len(result) <= 100
        assert "[Truncated]" in result


class TestIsAuthorized:
    """Tests for is_authorized function."""

    def test_empty_allowlist_allows_all(self):
        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            assert bot.is_authorized(12345) is True
            assert bot.is_authorized(99999) is True

    def test_allowlist_allows_listed_users(self):
        with patch.object(bot, 'ALLOWED_USER_IDS', [12345, 67890]):
            assert bot.is_authorized(12345) is True
            assert bot.is_authorized(67890) is True

    def test_allowlist_blocks_unlisted_users(self):
        with patch.object(bot, 'ALLOWED_USER_IDS', [12345, 67890]):
            assert bot.is_authorized(99999) is False


class TestFormatToolUse:
    """Tests for format_tool_use function."""

    def test_format_read(self):
        result = bot.format_tool_use("Read", {"file_path": "/path/to/file.py"})
        assert "Reading: file.py" == result

    def test_format_edit(self):
        result = bot.format_tool_use("Edit", {"file_path": "/path/to/file.py"})
        assert "Editing: file.py" == result

    def test_format_write(self):
        result = bot.format_tool_use("Write", {"file_path": "/path/to/new.py"})
        assert "Writing: new.py" == result

    def test_format_bash_short_command(self):
        result = bot.format_tool_use("Bash", {"command": "ls -la"})
        assert "Running: ls -la" == result

    def test_format_bash_long_command_truncated(self):
        long_cmd = "a" * 50
        result = bot.format_tool_use("Bash", {"command": long_cmd})
        assert len(result) < 50
        assert "..." in result

    def test_format_glob(self):
        result = bot.format_tool_use("Glob", {"pattern": "**/*.py"})
        assert "Searching: **/*.py" == result

    def test_format_grep(self):
        result = bot.format_tool_use("Grep", {"pattern": "TODO"})
        assert "Grep: TODO" == result

    def test_format_unknown_tool(self):
        result = bot.format_tool_use("UnknownTool", {})
        assert result == "UnknownTool"


class TestFormatDiff:
    """Tests for format_diff function."""

    def test_formats_basic_diff(self):
        result = bot.format_diff("old code", "new code", "/path/to/file.py")
        assert "file.py" in result
        # Should show removed and added lines with emoji
        assert "🟥" in result  # Removed
        assert "🟩" in result  # Added

    def test_includes_filename(self):
        result = bot.format_diff("old", "new", "/path/to/myfile.py")
        assert "myfile.py" in result

    def test_shows_context_lines(self):
        old_code = "line1\nline2\nold line\nline4\nline5"
        new_code = "line1\nline2\nnew line\nline4\nline5"
        result = bot.format_diff(old_code, new_code, "/file.py")
        # Should include context lines (line1, line2, line4, line5)
        assert "line1" in result or "line2" in result

    def test_truncates_long_content(self):
        old_content = "\n".join(f"line {i}" for i in range(500))
        new_content = "\n".join(f"new line {i}" for i in range(500))
        result = bot.format_diff(old_content, new_content, "/file.py")
        assert "truncated" in result.lower()

    def test_escapes_html(self):
        result = bot.format_diff("<script>", "</script>", "/file.py")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_no_changes_shows_message(self):
        result = bot.format_diff("same content", "same content", "/file.py")
        assert "No changes" in result


class TestFormatNewFile:
    """Tests for format_new_file function."""

    def test_formats_new_file(self):
        result = bot.format_new_file("print('hello')", "/path/to/new.py")
        assert "new.py" in result
        assert "(new file)" in result
        assert "print" in result

    def test_truncates_long_content(self):
        content = "x" * 600
        result = bot.format_new_file(content, "/file.py")
        assert "..." in result

    def test_escapes_html(self):
        result = bot.format_new_file("<script>alert('xss')</script>", "/file.py")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result


class TestSession:
    """Tests for Session dataclass."""

    def test_to_dict(self, sample_session):
        result = sample_session.to_dict()
        assert result["id"] == "20260102143022"
        assert result["cwd"] == "/home/user/project"
        assert result["created_at"] == "2026-01-02T14:30:22"
        assert result["resume_id"] == "abc-123-def"

    def test_from_dict(self):
        data = {
            "id": "20260102143022",
            "cwd": "/home/user/project",
            "created_at": "2026-01-02T14:30:22",
            "resume_id": "abc-123-def"
        }
        session = bot.Session.from_dict(data)
        assert session.id == "20260102143022"
        assert session.cwd == "/home/user/project"
        assert session.resume_id == "abc-123-def"

    def test_from_dict_with_missing_optional_fields(self):
        data = {"id": "123", "cwd": "/home"}
        session = bot.Session.from_dict(data)
        assert session.id == "123"
        assert session.cwd == "/home"
        assert session.resume_id is None


class TestSessionManager:
    """Tests for SessionManager class."""

    def test_get_or_create_new_session(self, session_manager):
        session = session_manager.get_or_create(12345)
        assert session is not None
        assert session.cwd == bot.DEFAULT_CWD

    def test_get_or_create_existing_session(self, session_manager):
        session1 = session_manager.get_or_create(12345)
        session2 = session_manager.get_or_create(12345)
        assert session1.id == session2.id

    def test_update_session(self, session_manager):
        session_manager.get_or_create(12345)
        session_manager.update(12345, cwd="/new/path", resume_id="new-resume")
        session = session_manager.get_or_create(12345)
        assert session.cwd == "/new/path"
        assert session.resume_id == "new-resume"

    def test_reset_session(self, session_manager):
        session1 = session_manager.get_or_create(12345)
        session_manager.update(12345, resume_id="old-resume-id")

        session2 = session_manager.reset(12345, cwd="/different/path")
        # Reset should clear resume_id and change cwd
        assert session2.cwd == "/different/path"
        assert session2.resume_id is None
        # The session object should be new (not the same reference)
        assert session2 is not session1

    def test_persistence(self, temp_sessions_file):
        # Create and save
        manager1 = bot.SessionManager(temp_sessions_file)
        session = manager1.get_or_create(12345)
        manager1.update(12345, resume_id="test-resume")

        # Load in new instance
        manager2 = bot.SessionManager(temp_sessions_file)
        loaded_session = manager2.get_or_create(12345)
        assert loaded_session.id == session.id
        assert loaded_session.resume_id == "test-resume"


class TestPendingEdit:
    """Tests for PendingEdit dataclass."""

    def test_pending_edit_creation(self, sample_pending_edit):
        assert sample_pending_edit.edit_id == "abc12345"
        assert sample_pending_edit.tool_name == "Edit"
        assert sample_pending_edit.file_path == "/home/user/project/main.py"

    def test_pending_edit_has_created_at(self, sample_pending_edit):
        assert sample_pending_edit.created_at is not None
        assert isinstance(sample_pending_edit.created_at, datetime)


class TestGetGitInfo:
    """Tests for get_git_info function."""

    def test_returns_empty_for_non_git_dir(self, temp_cwd):
        result = bot.get_git_info(temp_cwd)
        assert result == ""

    def test_returns_branch_for_git_dir(self, temp_cwd):
        # Initialize git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=temp_cwd, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=temp_cwd, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=temp_cwd, capture_output=True)

        # Create initial commit
        test_file = Path(temp_cwd) / "test.txt"
        test_file.write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_cwd, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=temp_cwd, capture_output=True)

        result = bot.get_git_info(temp_cwd)
        assert "master" in result or "main" in result

    def test_shows_uncommitted_changes(self, temp_cwd):
        import subprocess
        subprocess.run(["git", "init"], cwd=temp_cwd, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=temp_cwd, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=temp_cwd, capture_output=True)

        # Create initial commit
        test_file = Path(temp_cwd) / "test.txt"
        test_file.write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_cwd, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=temp_cwd, capture_output=True)

        # Create uncommitted change
        test_file.write_text("modified")

        result = bot.get_git_info(temp_cwd)
        assert "uncommitted" in result
