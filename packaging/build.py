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


# Bundled files the app never loads (it reads screenshots, not video, and
# draws its window with Qt Widgets: no QML, PDF, networking or translations).
# About 90 MB unpacked; every update downloads the zip, so this matters.
UNUSED_FILES = [
    "cv2/opencv_videoio_ffmpeg*.dll",  # video codecs
    "PySide6/opengl32sw.dll",  # software OpenGL fallback
    "PySide6/QtOpenGL.pyd", "PySide6/QtNetwork.pyd", "PySide6/Qt6Network.dll",
    "PySide6/Qt6Pdf.dll", "PySide6/Qt6Qml*.dll", "PySide6/Qt6Quick*.dll", "PySide6/Qt6VirtualKeyboard.dll",
    "PySide6/translations",
    "PySide6/plugins/tls", "PySide6/plugins/networkinformation", "PySide6/plugins/platforminputcontexts",
    "PySide6/plugins/generic", "PySide6/plugins/platforms/qdirect2d.dll",
    *(f"PySide6/plugins/imageformats/q{f}.dll" for f in ("pdf", "tiff", "webp", "tga", "wbmp", "icns")),
]


def prune(internal: Path) -> None:
    """Delete UNUSED_FILES, and any OpenSSL that Python's own ssl module
    doesn't load (PyInstaller can pick up a second copy from Qt)."""
    removed = 0
    for pattern in UNUSED_FILES:
        for path in internal.glob(pattern):
            removed += sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir()                 else path.stat().st_size
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    import pefile  # comes with PyInstaller

    used = set()
    for module in ("_ssl.pyd", "_hashlib.pyd"):
        pe = pefile.PE(str(internal / module), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        used |= {entry.dll.decode().lower() for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])}
        pe.close()
    for path in [*internal.glob("libssl-*.dll"), *internal.glob("libcrypto-*.dll")]:
        if path.name.lower() not in used:
            removed += path.stat().st_size
            path.unlink()
    print(f"pruned {removed / 2**20:.0f} MB of unused files")


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
    prune(APP / "_internal")
    # Pictures stay as plain files next to the exe so new ones can be added.
    shutil.copytree(ROOT / "templates", APP / "templates")
    shutil.copy(ROOT / "farmbot.example.toml", APP / "farmbot.example.toml")
    (APP / "START HERE.txt").write_text(START_HERE, encoding="utf-8")
    archive = shutil.make_archive(str(DIST / f"{NAME}-windows"), "zip", DIST, NAME)
    print(f"built {APP} and {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
