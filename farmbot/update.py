"""Is a newer COCFARM out? Asks GitHub for the latest release.

Only ever reads; the user downloads the new version from the release page.
Any failure (offline, private repository, rate limit) just means "no news".
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Callable

from farmbot import __version__

log = logging.getLogger(__name__)

TIMEOUT = 8


@dataclass
class Release:
    version: str
    url: str  # the release page


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "COCFARM"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def parse_version(text: str) -> tuple[int, ...]:
    """"v1.2.3" -> (1, 2, 3); anything unparseable -> ()."""
    numbers = re.findall(r"\d+", text)
    return tuple(int(n) for n in numbers[:3])


def newer_release(repo: str, current: str = __version__, get: Callable[[str], bytes] | None = None) -> Release | None:
    """The latest release of ``repo`` ("owner/name") if it is newer than
    ``current``, else ``None``."""
    if not repo:
        return None
    try:
        data = json.loads((get or _get)(f"https://api.github.com/repos/{repo}/releases/latest"))
    except Exception as exc:  # offline, private repo, no releases yet...
        log.debug("Update check failed: %s", exc)
        return None
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) > parse_version(current):
        return Release(tag.lstrip("v"), str(data.get("html_url") or f"https://github.com/{repo}/releases"))
    return None
