# Third-party components

Windows builds include Python 3.13.9, PySide6/Shiboken6/Qt 6.9.2, websocket-client 1.8.0, and psutil 7.0.0. PyInstaller 6.22.3 builds the executables.

Component copyright and license notices remain applicable; texts are included in `licenses`. The project's MIT license does not replace third-party licenses. Qt DLLs are separate files in `runtime/_internal` and may be replaced with compatible builds. Application source and rebuild instructions are included in the distribution.

Corresponding version sources:

- [Python 3.13.9](https://github.com/python/cpython/tree/v3.13.9)
- [PySide6 and Shiboken6 6.9.2](https://code.qt.io/cgit/pyside/pyside-setup.git/tree/?h=6.9.2)
- [Qt 6.9.2](https://code.qt.io/cgit/qt/qtbase.git/tree/?h=v6.9.2)
- [websocket-client 1.8.0](https://github.com/websocket-client/websocket-client/tree/v1.8.0)
- [psutil 7.0.0](https://github.com/giampaolo/psutil/tree/release-7.0.0)
- [PyInstaller 6.22.3](https://github.com/pyinstaller/pyinstaller/tree/v6.22.3)

The public build uses this project's installer and the supported Codex CLI marketplace commands. It does not bundle development-environment plugin scaffolding scripts or Codex itself.
