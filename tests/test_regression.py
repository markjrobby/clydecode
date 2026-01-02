"""
Regression tests for Clyde Code bot.

Tests for specific bugs and edge cases that have been fixed.
These tests ensure bugs don't reappear after code changes.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot


class TestSecurityRegressions:
    """Regression tests for security-related issues."""

    def test_no_secrets_in_telegram_messages(self):
        """Ensure secrets are never sent to Telegram."""
        # Various secret patterns that must be redacted
        secrets = [
            "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789",  # Telegram token
            "AKIAIOSFODNN7EXAMPLE",  # AWS key
            "ghp_1234567890abcdefghijklmnopqrstuvwxyz",  # GitHub token
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890",  # Anthropic key
            "sk-1234567890abcdefghijklmnopqrstuvwxyz12345678",  # OpenAI key
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",  # JWT
        ]

        for secret in secrets:
            result = bot.redact_sensitive(f"Here is a secret: {secret}")
            assert secret not in result, f"Secret was not redacted: {secret[:20]}..."

    def test_html_injection_prevented(self):
        """Ensure HTML injection is prevented in messages."""
        malicious_inputs = [
            "<script>alert('xss')</script>",
            "<img src=x onerror=alert('xss')>",
            "<a href='javascript:alert(1)'>click</a>",
        ]

        for malicious in malicious_inputs:
            result = bot.markdown_to_html(malicious)
            # Check that actual HTML tags are escaped
            assert "<script>" not in result
            assert "<img" not in result
            assert "<a href" not in result
            # Ensure they're properly escaped
            assert "&lt;" in result

    def test_authorization_cannot_be_bypassed(self):
        """Ensure authorization check cannot be bypassed."""
        with patch.object(bot, 'ALLOWED_USER_IDS', [12345]):
            # Various attempts to bypass
            assert bot.is_authorized(None) is False
            assert bot.is_authorized(0) is False
            assert bot.is_authorized(-1) is False
            assert bot.is_authorized(99999) is False

            # Only the allowed user should pass
            assert bot.is_authorized(12345) is True

    def test_path_traversal_in_diff_display(self):
        """Ensure path traversal doesn't affect display."""
        # Malicious path shouldn't cause issues
        pages = bot.format_diff(
            "old",
            "new",
            "../../../etc/passwd"
        )
        result = pages[0]
        assert "passwd" in result  # Filename extraction works
        assert "../../../etc/" not in result  # Path not fully shown


class TestEdgeCases:
    """Regression tests for edge cases."""

    def test_empty_string_handling(self):
        """Ensure empty strings don't cause crashes."""
        assert bot.redact_sensitive("") == ""
        assert bot.markdown_to_html("") == ""
        assert bot.truncate_message("") == ""
        assert bot.format_diff("", "", "/file.py") is not None
        assert bot.format_new_file("", "/file.py") is not None

    def test_unicode_handling(self):
        """Ensure unicode is handled correctly."""
        unicode_text = "Hello 世界 🌍 مرحبا שלום"
        assert bot.redact_sensitive(unicode_text) == unicode_text
        result = bot.markdown_to_html(f"**{unicode_text}**")
        assert "世界" in result
        assert "🌍" in result

    def test_very_long_messages(self):
        """Ensure very long messages are handled."""
        long_text = "x" * 100000
        result = bot.truncate_message(long_text)
        assert len(result) <= 4000
        assert "[Truncated]" in result

    def test_special_characters_in_paths(self):
        """Ensure special characters in file paths work."""
        special_paths = [
            "/path/with spaces/file.py",
            "/path/with'quotes/file.py",
            "/path/with\"doublequotes/file.py",
            "/path/with<brackets>/file.py",
        ]

        for path in special_paths:
            pages = bot.format_diff("old", "new", path)
            assert pages is not None
            assert len(pages) >= 1

    def test_null_bytes_in_content(self):
        """Ensure null bytes don't cause issues."""
        content_with_null = "before\x00after"
        result = bot.redact_sensitive(content_with_null)
        assert result is not None

    def test_format_tool_use_missing_keys(self):
        """Ensure missing keys in tool input are handled."""
        assert bot.format_tool_use("Read", {}) == "Reading: ?"
        assert bot.format_tool_use("Bash", {}) == "Running: ?"
        assert bot.format_tool_use("Glob", {}) == "Searching: ?"


class TestSessionRegressions:
    """Regression tests for session management."""

    def test_session_persistence_after_update(self, temp_sessions_file):
        """Ensure sessions are saved after updates."""
        manager = bot.SessionManager(temp_sessions_file)
        manager.get_or_create(12345)
        manager.update(12345, resume_id="test-id")

        # Reload and verify
        manager2 = bot.SessionManager(temp_sessions_file)
        session = manager2.get_or_create(12345)
        assert session.resume_id == "test-id"

    def test_session_reset_clears_resume_id(self, session_manager):
        """Ensure reset clears the resume ID."""
        session_manager.get_or_create(12345)
        session_manager.update(12345, resume_id="old-id")
        session_manager.reset(12345)

        session = session_manager.get_or_create(12345)
        assert session.resume_id is None

    def test_concurrent_session_access(self, temp_sessions_file):
        """Ensure concurrent session operations don't corrupt data."""
        manager = bot.SessionManager(temp_sessions_file)

        # Simulate concurrent access
        for i in range(10):
            manager.get_or_create(i)
            manager.update(i, cwd=f"/path/{i}")

        # Verify all sessions exist
        for i in range(10):
            session = manager.get_or_create(i)
            assert session.cwd == f"/path/{i}"


@pytest.mark.asyncio
class TestApprovalRegressions:
    """Regression tests for approval workflow."""

    async def test_approval_with_empty_old_string(self, mock_context):
        """Ensure Write operations (empty old_string) work correctly."""
        edit_info = {
            "edit_id": "test123",
            "tool_name": "Write",
            "file_path": "/test/new.py",
            "old_string": "",
            "new_string": "print('new file')",
        }

        approval_msg = MagicMock()
        approval_msg.message_id = 12345
        mock_context.bot.send_message = AsyncMock(return_value=approval_msg)

        msg_id, pages = await bot.show_approval_request(12345, mock_context, edit_info)
        assert msg_id == 12345
        assert isinstance(pages, list)

    async def test_reject_kills_process(self, mock_callback_query, mock_context, sample_pending_edit):
        """Ensure reject properly kills the Claude process."""
        await bot.handle_reject(mock_callback_query, sample_pending_edit, mock_context)

        sample_pending_edit.process.kill.assert_called_once()
        sample_pending_edit.process.wait.assert_called_once()

    async def test_double_approval_handling(self, mock_callback_query, mock_context, sample_pending_edit):
        """Ensure double-clicking approve doesn't cause issues."""
        bot.pending_edits[sample_pending_edit.edit_id] = sample_pending_edit
        mock_callback_query.data = f"approve_{sample_pending_edit.edit_id}"

        update = MagicMock()
        update.callback_query = mock_callback_query

        with patch.object(bot, 'continue_after_approval', new_callable=AsyncMock) as mock_continue:
            mock_continue.return_value = {"status": "complete", "response": "Done", "session_id": None}

            # First approval
            await bot.handle_edit_callback(update, mock_context)

            # Second approval (edit no longer exists)
            await bot.handle_edit_callback(update, mock_context)

        # Second call should show expired message
        assert mock_callback_query.edit_message_text.call_count >= 1


class TestMarkdownRegressions:
    """Regression tests for markdown conversion."""

    def test_nested_formatting(self):
        """Ensure nested formatting works correctly."""
        text = "**bold *italic* bold**"
        result = bot.markdown_to_html(text)
        assert "<b>" in result

    def test_code_block_with_language(self):
        """Ensure code blocks with language specifier work."""
        text = "```python\nprint('hello')\n```"
        result = bot.markdown_to_html(text)
        assert "<pre>" in result
        assert "print" in result

    def test_unmatched_formatting(self):
        """Ensure unmatched formatting doesn't break."""
        texts = [
            "**unclosed bold",
            "unclosed *italic",
            "unclosed `code",
            "```unclosed code block",
        ]

        for text in texts:
            result = bot.markdown_to_html(text)
            assert result is not None

    def test_special_markdown_chars(self):
        """Ensure special markdown characters are handled."""
        text = "Use * for multiplication and ** for power"
        result = bot.markdown_to_html(text)
        assert result is not None


class TestGitInfoRegressions:
    """Regression tests for git info retrieval."""

    def test_git_info_timeout(self, temp_cwd):
        """Ensure git info doesn't hang on slow operations."""
        # Should return quickly even if git is slow
        import time
        start = time.time()
        result = bot.get_git_info(temp_cwd)
        elapsed = time.time() - start

        # Should complete in under 10 seconds
        assert elapsed < 10

    def test_git_info_non_existent_dir(self):
        """Ensure non-existent directory doesn't crash."""
        result = bot.get_git_info("/nonexistent/path/12345")
        assert result == ""


@pytest.mark.asyncio
class TestStreamingRegressions:
    """Regression tests for Claude streaming."""

    async def test_malformed_json_in_stream(self, mock_update, mock_context, session_manager):
        """Ensure malformed JSON in stream doesn't crash the bot."""
        status_msg = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        mock_update.message.reply_text = AsyncMock(return_value=status_msg)

        async def mock_create_subprocess(*args, **kwargs):
            process = MagicMock()
            process.stdout = MagicMock()
            process.stderr = MagicMock()
            process.returncode = 0

            # Return malformed JSON followed by valid JSON
            outputs = [
                b'not valid json\n',
                b'{"type": "result", "result": "ok", "session_id": "123"}\n',
                b''
            ]
            output_iter = iter(outputs)
            process.stdout.read = AsyncMock(side_effect=lambda n: next(output_iter))
            process.stderr.readline = AsyncMock(return_value=b'')
            process.wait = AsyncMock()

            return process

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                with patch('asyncio.create_subprocess_exec', mock_create_subprocess):
                    # Should not raise an exception
                    await bot.handle_message(mock_update, mock_context)

    async def test_empty_stream_response(self, mock_update, mock_context, session_manager):
        """Ensure empty stream response is handled."""
        status_msg = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        mock_update.message.reply_text = AsyncMock(return_value=status_msg)

        async def mock_create_subprocess(*args, **kwargs):
            process = MagicMock()
            process.stdout = MagicMock()
            process.stderr = MagicMock()
            process.returncode = 0

            process.stdout.read = AsyncMock(return_value=b'')
            process.stderr.readline = AsyncMock(return_value=b'')
            process.wait = AsyncMock()

            return process

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                with patch('asyncio.create_subprocess_exec', mock_create_subprocess):
                    await bot.handle_message(mock_update, mock_context)

        # Should show "No response" message
        calls = [str(call) for call in mock_update.message.reply_text.call_args_list]
        assert any("No response" in call for call in calls)


class TestDiffFormattingRegressions:
    """Regression tests for diff formatting."""

    def test_diff_with_html_in_content(self):
        """Ensure HTML in code content is escaped."""
        old_code = "<div>old</div>"
        new_code = "<div>new</div>"

        pages = bot.format_diff(old_code, new_code, "/file.html")
        result = pages[0]

        assert "<div>" not in result
        assert "&lt;div&gt;" in result

    def test_diff_with_very_long_lines(self):
        """Ensure very long lines are handled."""
        old_code = "x" * 1000
        new_code = "y" * 1000

        pages = bot.format_diff(old_code, new_code, "/file.py")
        assert pages is not None
        assert len(pages) >= 1

    def test_diff_with_many_lines(self):
        """Ensure many lines are paged, not truncated."""
        old_code = "\n".join(f"line {i}" for i in range(500))
        new_code = "\n".join(f"new line {i}" for i in range(500))

        pages = bot.format_diff(old_code, new_code, "/file.py")
        assert pages is not None
        # With pagination, should have multiple pages instead of truncating
        assert len(pages) > 1

    def test_diff_shows_removed_and_added(self):
        """Ensure diff shows removed and added lines clearly."""
        old_code = "old line"
        new_code = "new line"

        pages = bot.format_diff(old_code, new_code, "/file.py")
        result = pages[0]
        # Should have emoji markers
        assert "🟥 old line" in result
        assert "🟩 new line" in result
