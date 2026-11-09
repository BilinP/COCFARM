"""Is the game in English? COCFARM finds buttons by their English words
("Battle", "Next", "Return Home"), so a game in another language looks to it
like a screen it doesn't know. Clash of Clans starts in the emulator's own
language, so Android's is the best sign COCFARM can read."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

PROPS = ("persist.sys.locale", "persist.sys.language", "ro.product.locale")
NAMES = {"fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
         "pl": "Polish", "ru": "Russian", "tr": "Turkish", "ar": "Arabic", "zh": "Chinese", "ja": "Japanese",
         "ko": "Korean", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ms": "Malay", "uk": "Ukrainian",
         "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "el": "Greek", "sv": "Swedish", "nb": "Norwegian",
         "da": "Danish", "fi": "Finnish", "he": "Hebrew", "fa": "Persian", "hi": "Hindi"}
NOT_ENGLISH = "isn't in English"  # in the message: friendly.py names the problem by it


def android_language(device) -> str | None:
    """Android's language in the emulator, like "fr-FR", or None if it can't be read."""
    if not hasattr(device, "_android_language"):
        found = None
        shell = getattr(device, "shell", None)
        if shell is not None:
            for prop in PROPS:
                try:
                    value = str(shell("getprop", prop)).strip()
                except Exception:  # noqa: BLE001 - any failure: not known
                    break
                if value:
                    found = value
                    break
        try:
            device._android_language = found  # read once per device
        except AttributeError:
            return found
    return getattr(device, "_android_language", None)


def name(tag: str) -> str:
    code = tag.replace("_", "-").split("-")[0].lower()
    return NAMES.get(code, tag)


def is_english(tag: str | None) -> bool:
    return not tag or tag.replace("_", "-").split("-")[0].lower() == "en"


def stuck_hint(device) -> str:
    """For a bot that recognises nothing: why that may be, or ""."""
    tag = android_language(device)
    if is_english(tag):
        return ""
    return (f". The emulator's Android is in {name(tag)}, so Clash of Clans probably {NOT_ENGLISH}: "
            "COCFARM reads the game's English buttons. Set the game to English in its Settings (gear) "
            "-> Language")


def warn_once(device) -> None:
    """A note in the log when the emulator isn't in English."""
    tag = android_language(device)
    if not is_english(tag) and not getattr(device, "_language_warned", False):
        log.warning("The emulator's Android is in %s (%s). COCFARM needs Clash of Clans in English: if the "
                    "game shows %s, set it to English in its Settings -> Language", name(tag), tag, name(tag))
        try:
            device._language_warned = True
        except AttributeError:
            pass
