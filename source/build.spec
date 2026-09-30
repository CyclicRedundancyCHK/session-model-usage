# Build a GUI launcher and a console query helper sharing one dependency folder.
from pathlib import Path

root = Path(SPECPATH)
a = Analysis([str(root / "main.py")], pathex=[str(root)],
             binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "matplotlib", "numpy", "pandas"],
             noarchive=False, optimize=1)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CodexSessionUsage",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          manifest=str(root / "windows.manifest"), icon=str(root / "icon.ico"))
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="session-usage",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True,
          manifest=str(root / "windows.manifest"), icon=str(root / "icon.ico"))
bundle = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="runtime")
