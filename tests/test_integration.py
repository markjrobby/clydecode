"""
Integration tests for Clyde Code bot.

Tests interactions between components and with external systems (mocked).
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot


@pytest.mark.asyncio
class TestMessageHandling:
    """Integration tests for message handling flow."""

    async def test_unauthorized_user_blocked(self, mock_update, mock_context):
        """Unauthorized users should receive error message."""
        with patch.object(bot, 'ALLOWED_USER_IDS', [99999]):
            await bot.handle_message(mock_update, mock_context)
            mock_update.message.reply_text.assert_called_with("Unauthorized.")

    async def test_empty_message_ignored(self, mock_update, mock_context):
        """Empty messages should be ignored."""
        mock_update.message.text = None
        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            await bot.handle_message(mock_update, mock_context)
            mock_context.bot.send_chat_action.assert_not_called()

    async def test_message_creates_status_message(self, mock_update, mock_context, session_manager):
        """Message should create a status message before processing."""
        mock_update.message.reply_text = AsyncMock(return_value=MagicMock(
            edit_text=AsyncMock(),
            delete=AsyncMock()
        ))

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                with patch.object(bot, 'run_claude_streaming', new_callable=AsyncMock) as mock_stream:
                    mock_stream.return_value = {
                        "status": "complete",
                        "response": "Test response",
                        "session_id": "sess-123"
                    }
                    await bot.handle_message(mock_update, mock_context)

        mock_context.bot.send_chat_action.assert_called()

    async def test_complete_response_sent_to_user(self, mock_update, mock_context, session_manager):
        """Complete responses should be sent back to user."""
        status_msg = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        mock_update.message.reply_text = AsyncMock(return_value=status_msg)

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                with patch.object(bot, 'run_claude_streaming', new_callable=AsyncMock) as mock_stream:
                    mock_stream.return_value = {
                        "status": "complete",
                        "response": "Here is my response",
                        "session_id": "sess-123"
                    }
                    await bot.handle_message(mock_update, mock_context)

        # Check that reply_text was called (once for status, once for response)
        assert mock_update.message.reply_text.call_count >= 1

    async def test_pending_approval_creates_buttons(self, mock_update, mock_context, session_manager):
        """Edit operations should create approval buttons."""
        status_msg = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        mock_update.message.reply_text = AsyncMock(return_value=status_msg)

        approval_msg = MagicMock()
        approval_msg.message_id = 12345
        mock_context.bot.send_message = AsyncMock(return_value=approval_msg)

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                with patch.object(bot, 'run_claude_streaming', new_callable=AsyncMock) as mock_stream:
                    mock_process = MagicMock()
                    mock_stream.return_value = {
                        "status": "pending_approval",
                        "edit_id": "test123",
                        "tool_name": "Edit",
                        "file_path": "/test/file.py",
                        "old_string": "old",
                        "new_string": "new",
                        "process": mock_process,
                        "session_id": "sess-123",
                        "cwd": "/test",
                        "user_id": 12345,
                    }
                    await bot.handle_message(mock_update, mock_context)

        # Check that approval message was sent
        mock_context.bot.send_message.assert_called()
        call_kwargs = mock_context.bot.send_message.call_args
        assert call_kwargs[1].get('reply_markup') is not None

    async def test_error_response_shown_to_user(self, mock_update, mock_context, session_manager):
        """Error responses should be shown to user."""
        status_msg = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        mock_update.message.reply_text = AsyncMock(return_value=status_msg)

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                with patch.object(bot, 'run_claude_streaming', new_callable=AsyncMock) as mock_stream:
                    mock_stream.return_value = {
                        "status": "error",
                        "message": "Something went wrong"
                    }
                    await bot.handle_message(mock_update, mock_context)

        # The error should be in one of the reply_text calls
        calls = mock_update.message.reply_text.call_args_list
        assert any("Something went wrong" in str(call) for call in calls)


@pytest.mark.asyncio
class TestApprovalWorkflow:
    """Integration tests for the approval workflow."""

    async def test_show_approval_request_for_edit(self, mock_context):
        """Edit operations should show diff with approve/reject buttons."""
        edit_info = {
            "edit_id": "test123",
            "tool_name": "Edit",
            "file_path": "/test/file.py",
            "old_string": "old code",
            "new_string": "new code",
        }

        approval_msg = MagicMock()
        approval_msg.message_id = 12345
        mock_context.bot.send_message = AsyncMock(return_value=approval_msg)

        msg_id, pages = await bot.show_approval_request(12345, mock_context, edit_info)

        assert msg_id == 12345
        assert isinstance(pages, list)
        assert len(pages) >= 1
        call_kwargs = mock_context.bot.send_message.call_args[1]
        assert "Approve" in str(call_kwargs['reply_markup'])
        assert "Reject" in str(call_kwargs['reply_markup'])

    async def test_show_approval_request_for_write(self, mock_context):
        """Write operations should show content with create/cancel buttons."""
        edit_info = {
            "edit_id": "test123",
            "tool_name": "Write",
            "file_path": "/test/new_file.py",
            "old_string": "",
            "new_string": "print('hello')",
        }

        approval_msg = MagicMock()
        approval_msg.message_id = 12345
        mock_context.bot.send_message = AsyncMock(return_value=approval_msg)

        msg_id, pages = await bot.show_approval_request(12345, mock_context, edit_info)

        assert msg_id == 12345
        assert isinstance(pages, list)
        call_kwargs = mock_context.bot.send_message.call_args[1]
        assert "Create" in str(call_kwargs['reply_markup'])
        assert "Cancel" in str(call_kwargs['reply_markup'])

    async def test_handle_edit_callback_approve(self, mock_callback_query, mock_context, sample_pending_edit):
        """Approving an edit should continue processing."""
        mock_callback_query.data = f"approve_{sample_pending_edit.edit_id}"
        bot.pending_edits[sample_pending_edit.edit_id] = sample_pending_edit

        # Mock continue_after_approval to return complete
        with patch.object(bot, 'continue_after_approval', new_callable=AsyncMock) as mock_continue:
            mock_continue.return_value = {
                "status": "complete",
                "response": "Done!",
                "session_id": "sess-123"
            }

            update = MagicMock()
            update.callback_query = mock_callback_query

            await bot.handle_edit_callback(update, mock_context)

        mock_callback_query.answer.assert_called()
        mock_callback_query.edit_message_text.assert_called()
        assert sample_pending_edit.edit_id not in bot.pending_edits

    async def test_handle_edit_callback_reject(self, mock_callback_query, mock_context, sample_pending_edit):
        """Rejecting an edit should kill the process."""
        mock_callback_query.data = f"reject_{sample_pending_edit.edit_id}"
        bot.pending_edits[sample_pending_edit.edit_id] = sample_pending_edit

        update = MagicMock()
        update.callback_query = mock_callback_query

        await bot.handle_edit_callback(update, mock_context)

        sample_pending_edit.process.kill.assert_called()
        mock_callback_query.edit_message_text.assert_called()
        assert "Rejected" in str(mock_callback_query.edit_message_text.call_args)

    async def test_handle_edit_callback_expired(self, mock_callback_query, mock_context):
        """Expired edits should show expiration message."""
        mock_callback_query.data = "approve_nonexistent"

        update = MagicMock()
        update.callback_query = mock_callback_query

        await bot.handle_edit_callback(update, mock_context)

        mock_callback_query.edit_message_text.assert_called_with("⚠️ This edit has expired.")

    async def test_handle_edit_callback_wrong_user(self, mock_callback_query, mock_context, sample_pending_edit):
        """Only the original user should be able to approve."""
        mock_callback_query.data = f"approve_{sample_pending_edit.edit_id}"
        mock_callback_query.from_user.id = 999999  # Different user
        bot.pending_edits[sample_pending_edit.edit_id] = sample_pending_edit

        update = MagicMock()
        update.callback_query = mock_callback_query

        await bot.handle_edit_callback(update, mock_context)

        mock_callback_query.answer.assert_called_with(
            "Only the requester can approve.",
            show_alert=True
        )
        # Edit should still be in pending
        assert sample_pending_edit.edit_id in bot.pending_edits

        # Cleanup
        del bot.pending_edits[sample_pending_edit.edit_id]


@pytest.mark.asyncio
class TestCommandHandlers:
    """Integration tests for command handlers."""

    async def test_cmd_start_authorized(self, mock_update, mock_context, session_manager):
        """Start command should show status for authorized users."""
        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_start(mock_update, mock_context)

        mock_update.message.reply_text.assert_called()
        call_args = str(mock_update.message.reply_text.call_args)
        assert "ready" in call_args.lower()

    async def test_cmd_start_unauthorized(self, mock_update, mock_context):
        """Start command should reject unauthorized users."""
        with patch.object(bot, 'ALLOWED_USER_IDS', [99999]):
            await bot.cmd_start(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_with("Unauthorized.")

    async def test_cmd_help(self, mock_update, mock_context):
        """Help command should show available commands."""
        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            await bot.cmd_help(mock_update, mock_context)

        mock_update.message.reply_text.assert_called()
        call_args = str(mock_update.message.reply_text.call_args)
        assert "/start" in call_args
        assert "/new" in call_args
        assert "/cwd" in call_args

    async def test_cmd_new_creates_session(self, mock_update, mock_context, session_manager, temp_cwd):
        """New command should create a fresh session."""
        mock_context.args = [temp_cwd]

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_new(mock_update, mock_context)

        mock_update.message.reply_text.assert_called()
        session = session_manager.get_or_create(mock_update.effective_user.id)
        assert session.cwd == temp_cwd

    async def test_cmd_new_invalid_path(self, mock_update, mock_context, session_manager):
        """New command should reject invalid paths."""
        mock_context.args = ["/nonexistent/path/12345"]

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_new(mock_update, mock_context)

        call_args = str(mock_update.message.reply_text.call_args)
        assert "not found" in call_args.lower()

    async def test_cmd_cwd_shows_current(self, mock_update, mock_context, session_manager):
        """CWD command without args should show current directory."""
        mock_context.args = []

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_cwd(mock_update, mock_context)

        mock_update.message.reply_text.assert_called()

    async def test_cmd_cwd_changes_directory(self, mock_update, mock_context, session_manager, temp_cwd):
        """CWD command with args should change directory."""
        mock_context.args = [temp_cwd]

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_cwd(mock_update, mock_context)

        session = session_manager.get_or_create(mock_update.effective_user.id)
        assert session.cwd == temp_cwd

    async def test_cmd_status(self, mock_update, mock_context, session_manager):
        """Status command should show session info."""
        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_status(mock_update, mock_context)

        mock_update.message.reply_text.assert_called()
        call_args = str(mock_update.message.reply_text.call_args)
        assert "Session" in call_args
        assert "Directory" in call_args

    async def test_cmd_git_runs_command(self, mock_update, mock_context, session_manager, temp_cwd):
        """Git command should execute git commands."""
        # Initialize git in temp dir
        import subprocess
        subprocess.run(["git", "init"], cwd=temp_cwd, capture_output=True)

        mock_context.args = ["status"]
        session_manager.get_or_create(mock_update.effective_user.id)
        session_manager.update(mock_update.effective_user.id, cwd=temp_cwd)

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_git(mock_update, mock_context)

        mock_update.message.reply_text.assert_called()

    async def test_cmd_git_no_args(self, mock_update, mock_context, session_manager):
        """Git command without args should show usage."""
        mock_context.args = []

        with patch.object(bot, 'ALLOWED_USER_IDS', []):
            with patch.object(bot, 'sessions', session_manager):
                await bot.cmd_git(mock_update, mock_context)

        call_args = str(mock_update.message.reply_text.call_args)
        assert "Usage" in call_args


@pytest.mark.asyncio
class TestContinueAfterApproval:
    """Integration tests for continue_after_approval function."""

    async def test_continues_to_completion(self, sample_pending_edit, mock_context):
        """After approval, should continue reading until completion."""
        # Mock process output
        output_lines = [
            b'{"type": "result", "result": "All done!", "session_id": "sess-456"}\n'
        ]
        sample_pending_edit.process.stdout.read = AsyncMock(side_effect=[
            output_lines[0],
            b''  # EOF
        ])

        result = await bot.continue_after_approval(sample_pending_edit, mock_context)

        assert result["status"] == "complete"
        assert result["response"] == "All done!"
        assert result["session_id"] == "sess-456"

    async def test_handles_chained_edits(self, sample_pending_edit, mock_context):
        """Should handle multiple edits in sequence."""
        # Mock process output with another edit
        output = b'{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/test/other.py", "old_string": "a", "new_string": "b"}}]}}\n'
        sample_pending_edit.process.stdout.read = AsyncMock(side_effect=[
            output,
            b''
        ])

        result = await bot.continue_after_approval(sample_pending_edit, mock_context)

        assert result["status"] == "pending_approval"
        assert result["tool_name"] == "Edit"
        assert result["file_path"] == "/test/other.py"


