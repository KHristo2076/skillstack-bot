import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_start_command_sends_reply():
    with patch("app.bot.Application") as MockApp:
        mock_application = MagicMock()
        MockApp.builder.return_value.token.return_value.build.return_value = mock_application

        from app.bot import BotService
        service = BotService()

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await service.start_command(update, context)

        update.message.reply_text.assert_called_once()
        call_kwargs = update.message.reply_text.call_args.kwargs
        assert "SkillStack" in call_kwargs["text"]
        assert call_kwargs["parse_mode"] == "Markdown"
