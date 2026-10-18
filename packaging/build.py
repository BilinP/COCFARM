"""Build COCFARM (farmbot) as a folder with COCFARM.exe in it, and zip it.

    pip install -e . pyinstaller
    python packaging/build.py

Run it on Windows to get a Windows exe (PyInstaller can't cross-compile);
the GitHub Actions "windows-exe" workflow does that on every push, and
publishes the zip as a GitHub Release for every v* tag. The result is dist/COCFARM/ and
dist/COCFARM-windows.zip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "COCFARM"
APP = DIST / NAME

UNUSED_QT = [f"PySide6.{m}" for m in (
    "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel", "QtWebSockets",
    "QtMultimedia", "QtMultimediaWidgets", "QtSpatialAudio", "QtQuick", "QtQuickWidgets", "QtQuick3D",
    "QtQml", "QtQuickControls2", "QtPositioning", "QtLocation", "QtPdf", "QtPdfWidgets", "Qt3DCore",
    "Qt3DRender", "QtDataVisualization", "QtGraphs", "QtBluetooth", "QtNfc", "QtSensors",
    "QtSerialPort", "QtSql", "QtTest", "QtDesigner", "QtHelp", "QtUiTools", "QtTextToSpeech",
    "QtRemoteObjects", "QtScxml", "QtStateMachine", "QtHttpServer", "QtNetworkAuth", "QtOpenGLWidgets",
)] + ["qfluentwidgets.multimedia", "PIL", "tkinter"]

START_HERE = """COCFARM
=======

1. Install BlueStacks 5 or LDPlayer 9, and Clash of Clans in it. COCFARM
   sets the emulator up by itself: 860 x 672 display at 240 DPI, and ADB
   on. It restarts the emulator once the first time to do that.
2. Open Clash of Clans in the emulator.
3. Double-click COCFARM.exe (Windows may warn about an unknown publisher:
   More info > Run anyway).
4. COCFARM opens on Setup: follow its four steps (emulator, Check now,
   the loot you want, Start).

Your settings (farmbot.toml), button pictures (templates), attack log
(attacks.csv) and error screenshots (screens) are kept in this folder.
If the window does not open, look in farmbot.log here.

Automating Clash of Clans breaks Supercell's Terms of Service.
"""


def main() -> int:
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", NAME, "--windowed", "--onedir",
        "--icon", str(ROOT / "packaging" / "cocfarm.ico"),
        "--add-data", f"{ROOT / 'farmbot' / 'assets'}{os.pathsep}farmbot/assets",
        "--add-data", f"{ROOT / 'farmbot' / 'profiles'}{os.pathsep}farmbot/profiles",
        "--collect-all", "qfluentwidgets",
        "--collect-all", "qframelesswindow",
        "--collect-submodules", "farmbot",
        # Qt add-ons the window never uses; Fluent Widgets' optional media
        # player would otherwise pull in half a gigabyte of Qt.
        *(arg for m in UNUSED_QT for arg in ("--exclude-module", m)),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        str(ROOT / "packaging" / "farmbot_app.py"),
    ], check=True, cwd=ROOT)
    # Pictures stay as plain files next to the exe so new ones can be added.
    shutil.copytree(ROOT / "templates", APP / "templates")
    shutil.copy(ROOT / "farmbot.example.toml", APP / "farmbot.example.toml")
    (APP / "START HERE.txt").write_text(START_HERE, encoding="utf-8")
    archive = shutil.make_archive(str(DIST / f"{NAME}-windows"), "zip", DIST, NAME)
    print(f"built {APP} and {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
