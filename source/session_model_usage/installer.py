from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from .platform_win import find_app, hidden_startup


def write_local_marketplace(root: Path) -> Path:
    """Register this product through its own marketplace, preserving others."""
    folder = root / ".agents/plugins"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "marketplace.json"
    value = {"name": "session-model-usage", "interface": {"displayName": "Codex 会话模型用量"},
        "plugins": [{"name": "session-model-usage", "source": {"source": "local", "path": str(root.resolve())},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity"}]}
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return path


def find_cli() -> str:
    found = shutil.which("codex")
    if found:
        return found
    folder = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    candidates = sorted(folder.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return str(candidates[0])
    candidate = find_app().parent / "resources/codex.exe"
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("未找到 Codex CLI，请确认 Codex 已安装")


def install(source: Path) -> dict:
    source = source.resolve()
    home = Path.home().resolve()
    destination = (home / "plugins/session-model-usage").resolve()
    if not destination.is_relative_to(home):
        raise RuntimeError("个人插件目标经解析后不在当前用户目录内")
    for component in (".codex-plugin/plugin.json", "skills", "scripts", "runtime/session-usage.exe",
                      "runtime/CodexSessionUsage.exe"):
        if not (source / component).exists():
            raise RuntimeError(f"安装包缺少组件：{component}")
    manifest = json.loads((source / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != "session-model-usage":
        raise RuntimeError("插件清单名称不匹配")
    cli = find_cli()
    if destination.exists() and source != destination:
        raise RuntimeError(f"目标已存在，未覆盖任何文件：{destination}。请在 Codex 中使用插件更新流程。")
    if not destination.exists():
        destination.mkdir(parents=True)
        for component in (".codex-plugin", "skills", "scripts", "runtime", "source", "assets", "docs"):
            if not (source / component).exists():
                continue
            shutil.copytree(source / component, destination / component, dirs_exist_ok=True)
        if (source / "licenses").is_dir():
            shutil.copytree(source / "licenses", destination / "licenses", dirs_exist_ok=True)
        for name in ("README.md", "LICENSE", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"):
            if (source / name).exists():
                shutil.copy2(source / name, destination / name)
    write_local_marketplace(destination)
    registered = subprocess.run([cli, "plugin", "marketplace", "add", str(destination), "--json"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=120, startupinfo=hidden_startup())
    if registered.returncode:
        raise RuntimeError(f"注册本插件市场失败，已保留安装目录：{registered.stderr.strip()[:400]}")
    name = "session-model-usage"
    result = subprocess.run([cli, "plugin", "add", f"session-model-usage@{name}"],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=120,
                            startupinfo=hidden_startup())
    if result.returncode:
        raise RuntimeError(f"Codex 插件安装失败，已保留个人插件目录：{result.stderr.strip()[:400]}")
    menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    menu.mkdir(parents=True, exist_ok=True)
    shortcut = menu / "Codex 会话用量.lnk"
    def literal(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"
    command = ("$ErrorActionPreference='Stop'; $shell=New-Object -ComObject WScript.Shell; "
               f"$link=$shell.CreateShortcut({literal(shortcut)}); "
               f"$link.TargetPath={literal(destination / 'runtime/CodexSessionUsage.exe')}; "
               f"$link.WorkingDirectory={literal(destination / 'runtime')}; "
               "$link.Description='Codex session token usage'; $link.Save()")
    linked = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, timeout=15, startupinfo=hidden_startup())
    return {"status": "installed", "plugin": "session-model-usage", "marketplace": name,
            "source_path": str(destination), "shortcut": str(shortcut) if not linked.returncode else None,
            "message": "已安装。首次使用请手动退出普通 Codex，再从开始菜单打开“Codex 会话用量”。插件技能在新会话中加载。"}
