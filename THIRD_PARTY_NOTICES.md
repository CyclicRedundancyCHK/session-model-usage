# Third-party components

The combined native quota frontend derives from [CodexQuotaTray](https://github.com/SYD-Official/CodexQuotaTray), commit `a89f33f0aa8395d421567f83f7720beb7c08b7d3` (v2.6.0), Copyright (c) 2026 SYD-Official, under MIT. The full license is included in `licenses/CodexQuotaTray-MIT.txt`, source and provenance in `source/quota_tray`. This derivative is maintained by CyclicRedundancyCHK and is not an official CodexQuotaTray release. The native component uses the Windows .NET Framework 4.8 runtime installed with Windows.

From 0.2.1-local.1, the taskbar quota strip and quota overview presentation are redesigned for this combined companion. Shared quota readers, settings and supporting native code retain their upstream provenance and license.

Windows builds include Python 3.13.9, PySide6/Shiboken6/Qt 6.9.2, websocket-client 1.8.0, and psutil 7.0.0, and comtypes 1.4.17. PyInstaller 6.22.3 builds the executables.

Component copyright and license notices remain applicable; texts are included in `licenses`. The project's MIT license does not replace third-party licenses. Qt DLLs are separate files in `runtime/_internal` and may be replaced with compatible builds. Application source and rebuild instructions are included in the distribution.

Corresponding version sources:

- [Python 3.13.9](https://github.com/python/cpython/tree/v3.13.9)
- [PySide6 and Shiboken6 6.9.2](https://code.qt.io/cgit/pyside/pyside-setup.git/tree/?h=6.9.2)
- [Qt 6.9.2](https://code.qt.io/cgit/qt/qtbase.git/tree/?h=v6.9.2)
- [websocket-client 1.8.0](https://github.com/websocket-client/websocket-client/tree/v1.8.0)
- [psutil 7.0.0](https://github.com/giampaolo/psutil/tree/release-7.0.0)
- [comtypes 1.4.17](https://github.com/enthought/comtypes/tree/v1.4.17)
- [PyInstaller 6.22.3](https://github.com/pyinstaller/pyinstaller/tree/v6.22.3)

The public build uses this project's installer and the supported Codex CLI marketplace commands. It does not bundle development-environment plugin scaffolding scripts or Codex itself.
