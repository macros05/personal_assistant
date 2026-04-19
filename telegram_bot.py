"""Telegram bot: receives webhook updates and replies via the agent."""
import json
import logging
from typing import Optional

from telegram import Bot, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError

from agent import run_agent

log = logging.getLogger("telegram_bot")

_MAX_MSG_LEN = 4000  # Telegram limit is 4096; keep a small margin


class TelegramBot:
    """Wraps python-telegram-bot for webhook-based operation inside FastAPI."""

    def __init__(self, token: str, webhook_url: str) -> None:
        self._bot         = Bot(token=token)
        self._webhook_url = webhook_url.rstrip("/")
        self._client      = None
        self._model: Optional[str] = None

    def set_agent(self, client, model: str) -> None:
        """Inject the Gemini client and model name after construction."""
        self._client = client
        self._model  = model

    async def setup_webhook(self) -> None:
        """Register this server's webhook URL with Telegram."""
        url = f"{self._webhook_url}/telegram/webhook"
        try:
            await self._bot.set_webhook(url=url)
            log.info("Telegram webhook registered → %s", url)
        except TelegramError as e:
            log.error("Telegram webhook registration failed: %s", e)

    async def delete_webhook(self) -> None:
        try:
            await self._bot.delete_webhook()
            log.info("Telegram webhook deleted")
        except TelegramError as e:
            log.warning("Could not delete webhook: %s", e)

    async def handle_update(self, update_data: dict) -> None:
        """Entry point called by the FastAPI webhook route."""
        update = Update.de_json(update_data, self._bot)

        if not update.message:
            return

        msg = update.message
        chat_id = msg.chat_id

        # Handle /start command
        if msg.text and msg.text.strip() == "/start":
            await self._send(chat_id, "👋 Hola Marcos. Escríbeme lo que necesites.")
            return

        if not msg.text:
            await self._send(chat_id, "Solo proceso mensajes de texto por ahora.")
            return

        user_text = msg.text.strip()
        log.info("Telegram message from chat %s: %s", chat_id, user_text[:80])

        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError:
            pass

        reply = await self._collect_agent_response(user_text)
        await self._send(chat_id, reply)

    async def _collect_agent_response(self, user_message: str) -> str:
        """Run the agent loop and accumulate streamed text chunks."""
        full_text = ""
        try:
            async for sse_line in run_agent(user_message, self._client, self._model):
                if not sse_line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(sse_line[6:])
                    if evt.get("text"):
                        full_text += evt["text"]
                except (json.JSONDecodeError, KeyError):
                    pass
        except Exception as e:
            log.exception("Agent error while handling Telegram message")
            return f"⚠️ Error interno: {e}"

        return full_text.strip() or "Sin respuesta."

    async def _send(self, chat_id: int, text: str) -> None:
        """Send a message, splitting it if it exceeds Telegram's limit."""
        for i in range(0, max(1, len(text)), _MAX_MSG_LEN):
            chunk = text[i : i + _MAX_MSG_LEN]
            try:
                await self._bot.send_message(chat_id=chat_id, text=chunk)
            except TelegramError as e:
                log.error("Failed to send Telegram message to %s: %s", chat_id, e)
