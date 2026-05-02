"""Telegram alerts. Optional — falls back to no-op if not configured."""
from __future__ import annotations

import requests
from loguru import logger

from .config import TelegramConfig


class Alerts:
    """Always-callable alert sink. Becomes a no-op when telegram is unconfigured."""

    def __init__(self, config: TelegramConfig):
        self.config = config

    def send(self, text: str) -> None:
        if not self.config.enabled:
            return
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        try:
            r = requests.post(
                url,
                json={"chat_id": self.config.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if r.status_code >= 400:
                logger.warning(f"Telegram returned {r.status_code}: {r.text}")
        except Exception as e:
            # Never let an alert failure break trading
            logger.warning(f"Telegram send failed: {e}")
