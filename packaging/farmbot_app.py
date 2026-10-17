"""Entry point of COCFARM.exe (built by packaging/build.py).

Double-clicking the exe opens the window. The exe works from its own
folder, so farmbot.toml, templates/, attacks.csv and screens/ sit next to
it whatever the shortcut's start folder. With no console, output goes to
farmbot.log there instead.
"""

import os
import sys
import traceback


def main() -> int:
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))
    if sys.stdout is None or sys.stderr is None:  # windowed exe: no console
        log = open("farmbot.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    args = sys.argv[1:]
    try:
        if args == ["--self-test"]:
            return self_test()
        from farmbot.cli import main as cli_main

        return cli_main(args or ["gui"])
    except Exception:
        traceback.print_exc()
        return 1


def self_test() -> int:
    """Build the whole window and read the shipped pictures, then exit.
    Run by CI against the built exe to catch missing files or modules."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from farmbot import catalog
    from farmbot.gui import FarmbotWindow, PAGES, create_app
    from farmbot.vision import TemplateLibrary

    app = create_app()
    window = FarmbotWindow("selftest.toml")
    for page in PAGES:
        window.show_page(page)
        app.processEvents()
    window.close()
    library = TemplateLibrary("templates")
    assert not catalog.missing(library, required_only=True), "shipped pictures missing"
    import cv2  # noqa: F401  (OpenCV's binary must load)

    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
