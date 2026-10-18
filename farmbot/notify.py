"""Messages to your phone through a Discord webhook.

Sent in the background so a slow or failed request never holds up the bot;
failures are only logged. Nothing is sent unless notify.discord_webhook is set.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from typing import Callable

from farmbot.config import NotifyConfig

log = logging.getLogger(__name__)

TIMEOUT = 10  # seconds per request

Post = Callable[[str, bytes], None]  # url, JSON body


def _post(url: str, body: bytes) -> None:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "COCFARM"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read()


class Notifier:
    def __init__(self, cfg: NotifyConfig, post: Post = _post, background: bool = True):
        self.cfg = cfg
        self._post = post
        self._background = background

    @property
    def configured(self) -> bool:
        return bool(self.cfg.discord_webhook)

    def send(self, text: str, event: str = "") -> None:
        """Post ``text`` to the webhook, if ``event`` (a NotifyConfig switch
        name, e.g. "on_storage_full") is turned on."""
        if not self.configured or (event and not getattr(self.cfg, event, True)):
            return
        if self._background:
            threading.Thread(target=self._send, args=(text,), daemon=True).start()
        else:
            self._send(text)

    def _send(self, text: str) -> bool:
        try:
            self._post(self.cfg.discord_webhook, json.dumps({"content": f"COCFARM: {text}"}).encode())
            return True
        except Exception as exc:  # network trouble: never stop the bot for it
            log.warning("Discord message not sent: %s", exc)
            return False

    def test(self) -> bool:
        """Send a test message now (not in the background). True if Discord took it."""
        return self.configured and self._send("test message: notifications work.")
