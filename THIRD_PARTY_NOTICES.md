# Third-party notices

COCFARM is free software under the GNU General Public License v3 or later
(see [`LICENSE`](LICENSE)). The Windows download (`COCFARM-windows.zip`)
also contains the libraries below, each under its own license. Their
license texts are in the [`licenses`](licenses) folder and in `LICENSE`.
None of them has been modified.

## Qt for Python (PySide6, Shiboken6) and Qt

- Copyright (C) The Qt Company Ltd. and other contributors.
- License: GNU Lesser General Public License v3 ([`licenses/LGPL-3.0.txt`](licenses/LGPL-3.0.txt),
  which adds to the GPL v3 in [`LICENSE`](LICENSE)).
- Source: <https://code.qt.io/> and <https://download.qt.io/official_releases/QtForPython/>.
- The Qt libraries ship as separate files in `_internal\PySide6`, so you can
  replace them with your own build of the same version.

## PySide6-Fluent-Widgets (qfluentwidgets)

- Copyright (C) zhiyiYo.
- License: GNU General Public License v3 ([`LICENSE`](LICENSE)). It's free
  for non-commercial use under the GPL v3; COCFARM is free and non-commercial.
- Source: <https://github.com/zhiyiYo/PyQt-Fluent-Widgets>.

## PySideSix-Frameless-Window (qframelesswindow)

- Copyright (C) zhiyiYo.
- License: GNU Lesser General Public License v3 ([`licenses/LGPL-3.0.txt`](licenses/LGPL-3.0.txt)).
- Source: <https://github.com/zhiyiYo/PyQt-Frameless-Window>.

## OpenCV

- Copyright (C) the OpenCV team and contributors.
- License: Apache License 2.0 ([`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt)).
- Source: <https://github.com/opencv/opencv>.
- Packaged by opencv-python (MIT License, Copyright (c) Olli-Pekka Heinisuo):
  <https://github.com/opencv/opencv-python>. Its license, and the notices
  for the libraries OpenCV includes, are in `licenses/opencv-python` in the
  download.

## Also included

- **Python**: Python Software Foundation License. <https://docs.python.org/3/license.html>
- **NumPy**: BSD 3-Clause License (text in `licenses/numpy` in the download). <https://numpy.org>
- **pywin32**: Python Software Foundation License. <https://github.com/mhammond/pywin32>
- **PyInstaller** bootloader, which starts `COCFARM.exe`: GNU GPL v2 or later
  with an exception for the programs it bundles. <https://pyinstaller.org>
