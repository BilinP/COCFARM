"""Reading and saving pictures at any path.

OpenCV's own ``imread``/``imwrite`` can't open a path with letters outside
plain English on Windows (a user folder like ``C:\\Users\\Jérôme``): they
return nothing. Python reads and writes the file, OpenCV only (de)codes it.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def read(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """The picture at ``path``, or ``None`` if it's missing or unreadable."""
    try:
        data = np.fromfile(str(path), np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def write(path: str | Path, image: np.ndarray, params: list[int] | tuple[int, ...] = ()) -> bool:
    """Save ``image`` to ``path`` (in the format its extension names)."""
    ok, data = cv2.imencode(Path(path).suffix or ".png", image, list(params))
    if not ok:
        return False
    try:
        data.tofile(str(path))
    except OSError:
        return False
    return True
