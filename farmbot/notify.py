"""Messages to your phone through a Discord webhook.

Each message is a Discord embed: a coloured card with a title, a line on what
happened, the attacks, and the loot drawn as a small card with the game's own
gold, elixir and dark elixir sprites, signed with the COCFARM logo. Sent in
the background so a slow or failed request never holds up the bot; failures
are only logged. Nothing is sent unless notify.discord_webhook is set.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

from farmbot import __version__
from farmbot.config import NotifyConfig

log = logging.getLogger(__name__)

TIMEOUT = 10  # seconds per request
ASSETS = Path(__file__).parent / "assets"
LOGO = ASSETS / "cocfarm.png"
LOGO_NAME = "cocfarm.png"  # the attachment the embed's author icon points at
LOGO_SIZE = 128  # px: the full logo is far bigger than Discord shows it
LOOT_NAME = "loot.png"  # the attachment the embed's image points at

# The card's side stripe, by what happened.
COLOURS = {
    "good": 0x2ECC71,  # storage full: the goal reached
    "info": 0x3498DB,  # switching villages, a test
    "warn": 0xF39C12,  # restarting the emulator
    "error": 0xE74C3C,  # stopped by an error
    "summary": 0xF1C40F,  # the daily summary, a normal stop
}
RESOURCES = (("gold", "🪙"), ("elixir", "💧"), ("dark", "⚫"))  # emoji: when the loot card can't be drawn

Post = Callable[[str, dict, dict], None]  # webhook url, Discord message (JSON payload), files by name


def compact(value: int) -> str:
    """1,950,000 -> "1.95M", 96,400 -> "96.4k", 2,100,000 -> "2.1M"."""
    sign, size = ("-" if value < 0 else ""), abs(value)
    if size < 1_000:
        return f"{sign}{size}"
    for scale, unit in ((1_000, "k"), (1_000_000, "M"), (1_000_000_000, "B")):
        text = f"{size / scale:.3g}"
        if float(text) < 1_000:
            return f"{sign}{text}{unit}"
    return f"{sign}{size / 1_000_000_000:.0f}B"


@lru_cache(maxsize=1)
def _logo() -> bytes | None:
    try:
        import cv2

        image = cv2.imread(str(LOGO), cv2.IMREAD_UNCHANGED)
        image = cv2.resize(image, (LOGO_SIZE, LOGO_SIZE), interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", image)
        return png.tobytes() if ok else None
    except Exception:  # no logo: the message goes without it
        return None


def loot_image(loot) -> bytes | None:
    """The loot as a small dark card: gold, elixir and dark elixir side by
    side, each sprite over its amount. Needs the window's Qt (for its
    fonts); None without it."""
    try:
        from PySide6.QtCore import QBuffer, QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QImage, QPainter

        if QGuiApplication.instance() is None:
            return None
        icon, pad, gap, text_h = 40, 14, 6, 28
        font = QFont("Segoe UI")
        font.setPixelSize(22)
        font.setBold(True)
        texts = [compact(getattr(loot, key)) for key, _ in RESOURCES]
        column = max(icon, max(QFontMetrics(font).horizontalAdvance(t) for t in texts)) + 28
        width = pad * 2 + column * len(texts)
        height = pad * 2 + icon + gap + text_h
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        p = QPainter(image)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#232428"))
        p.drawRoundedRect(QRectF(0, 0, width, height), 10, 10)
        p.setFont(font)
        p.setPen(QColor("#F2F3F5"))
        for i, ((key, _), text) in enumerate(zip(RESOURCES, texts)):
            x = pad + i * column
            sprite = QImage(str(ASSETS / f"{key}.png"))
            p.drawImage(QRectF(x + (column - icon) / 2, pad, icon, icon), sprite)
            p.drawText(QRectF(x, pad + icon + gap, column, text_h), Qt.AlignCenter, text)
        p.end()
        buffer = QBuffer()
        buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())
    except Exception as exc:  # the emoji lines stand in
        log.debug("Loot card not drawn: %s", exc)
        return None


def _post(url: str, payload: dict, files: dict) -> None:
    """Send the message, with the logo and any images attached."""
    headers = {"User-Agent": f"COCFARM/{__version__}"}
    files = dict(files)
    logo = _logo()
    if logo is None:
        for embed in payload.get("embeds", []):
            embed.get("author", {}).pop("icon_url", None)
    else:
        files[LOGO_NAME] = logo
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
             f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode()]
    for i, (name, data) in enumerate(files.items()):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{name}"\r\n'
                     f"Content-Type: image/png\r\n\r\n".encode() + data)
    body = b"\r\n".join(parts) + f"\r\n--{boundary}--\r\n".encode()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read()


def message(title: str, text: str, kind: str = "info", fields: list[tuple[str, str]] = (),
            image: bool = False, now: datetime | None = None) -> dict:
    """The Discord message for one notification: a single embed. Each field
    gets its own line; ``image`` shows the attached loot card."""
    embed = {
        "author": {"name": "COCFARM", "icon_url": f"attachment://{LOGO_NAME}"},
        "title": title,
        "description": text,
        "color": COLOURS.get(kind, COLOURS["info"]),
        "footer": {"text": f"COCFARM {__version__}"},
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
    }
    if fields:
        embed["fields"] = [{"name": name, "value": value, "inline": False} for name, value in fields]
    if image:
        embed["image"] = {"url": f"attachment://{LOOT_NAME}"}
    return {"username": "COCFARM", "embeds": [embed], "allowed_mentions": {"parse": []}}


def loot_parts(attacks: int | None, loot) -> tuple[list[tuple[str, str]], dict]:
    """The attacks as a field, and the loot as a card image (or, without
    one, as emoji lines in a field)."""
    fields = [] if attacks is None else [("⚔️ Attacks", f"{attacks:,}")]
    files = {}
    if loot is not None:
        card = loot_image(loot)
        if card:
            files[LOOT_NAME] = card
        else:
            fields.append(("Loot", "\n".join(f"{emoji} {compact(getattr(loot, key))}" for key, emoji in RESOURCES)))
    return fields, files


class Notifier:
    def __init__(self, cfg: NotifyConfig, post: Post = _post, background: bool = True):
        self.cfg = cfg
        self._post = post
        self._background = background

    @property
    def configured(self) -> bool:
        return bool(self.cfg.discord_webhook)

    def send(self, title: str, text: str = "", event: str = "", kind: str = "info",
             attacks: int | None = None, loot=None) -> None:
        """Post a card to the webhook, if ``event`` (a NotifyConfig switch
        name, e.g. "on_storage_full") is turned on."""
        if not self.configured or (event and not getattr(self.cfg, event, True)):
            return
        fields, files = loot_parts(attacks, loot)
        payload = message(title, text, kind, fields, image=bool(files))
        if self._background:
            threading.Thread(target=self._send, args=(payload, files), daemon=True).start()
        else:
            self._send(payload, files)

    def _send(self, payload: dict, files: dict | None = None) -> bool:
        try:
            self._post(self.cfg.discord_webhook, payload, files or {})
            return True
        except Exception as exc:  # network trouble: never stop the bot for it
            log.warning("Discord message not sent: %s", exc)
            return False

    def test(self) -> bool:
        """Send a test message now (not in the background). True if Discord took it."""
        return self.configured and self._send(message(
            "Notifications work", "COCFARM can post to this channel. You'll hear from it here when storage is "
            "full, when it stops, when it restarts the emulator, and with a daily summary.", "info"))
