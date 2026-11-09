"""The one emulator set-up COCFARM runs on, whichever emulator it is.

Every instance is put back to this when the bot connects (restarting the
emulator once if anything differs), so a setting changed by hand in the
emulator, or left different on another emulator, never changes what the bot
sees. They aren't settings: the pictures, the starting profile and the
deploy lines were all made on this screen.

It is the MuMu Player instance the 1.5 releases were tested on, as the
phone BlueStacks starts with (its "OnePlus 10 Pro" device profile). Only the
phone's maker and model are copied, never its identifiers (IMEI, Android ID,
MAC and so on): each instance keeps its own, so farms don't look like one
phone.
"""

from __future__ import annotations

WIDTH = 860
HEIGHT = 672
DPI = 240
CPUS = 2
MEMORY = 4096  # MB
FPS = 60  # frames a second; vertical sync and "high frame rate" modes off
MANUFACTURER = "OnePlus"  # ro.product.manufacturer, as a real OnePlus 10 Pro reports it
BRAND = "OnePlus"  # how MuMu Player lists the maker
MODEL = "NE2211"  # ro.product.model of a OnePlus 10 Pro
MODEL_NAME = "OnePlus 10 Pro"  # how MuMu Player lists the model
BLUESTACKS_PROFILE = "optp"  # BlueStacks' device profile code for it (reports OnePlus NE2211)


def describe() -> str:
    return f"{WIDTH}x{HEIGHT} {DPI} DPI, {CPUS} CPU cores, {MEMORY} MB, {FPS} FPS, {BRAND} {MODEL_NAME} ({MODEL})"
