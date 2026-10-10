from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import psutil

from .platform_win import find_app, hidden_startup
from .diagnostics import record


def write_local_marketplace(root: Path) -> Path:
    """Register this product through its own marketplace, preserving others."""
    folder = root / ".agents/plugins"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "marketplace.json"
    value = {"name": "session-model-usage", "interface": {"displayName": "Codex 会话模型用量"},
        "plugins": [{"name": "session-model-usage", "source": {"source": "local", "path": "./"},
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
    if (home / 'plugins/session-model-usage').is_symlink():
        raise RuntimeError('升级目标是链接，未覆盖文件')
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_release(source, destination)
    write_local_marketplace(destination)
    registered = subprocess.run([cli, "plugin", "marketplace", "add", str(destination), "--json"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=120, startupinfo=hidden_startup(), cwd=home)
    if registered.returncode:
        raise RuntimeError(f"注册本插件市场失败，已保留安装目录：{registered.stderr.strip()[:400]}")
    name = "session-model-usage"
    result = subprocess.run([cli, "plugin", "add", f"session-model-usage@{name}"],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=120,
                            startupinfo=hidden_startup(), cwd=home)
    if result.returncode:
        raise RuntimeError(f"Codex 插件安装失败，已保留个人插件目录：{result.stderr.strip()[:400]}")
    shortcut = create_shortcut(destination)
    return {"status": "installed", "plugin": "session-model-usage", "marketplace": name,
            "source_path": str(destination), "shortcut": str(shortcut) if shortcut else None,
            "message": "已安装。从开始菜单打开“Codex 会话用量”。插件技能在新会话中加载。"}


def create_shortcut(destination: Path) -> Path | None:
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
    return shortcut if not linked.returncode else None


def cli_json(cli, *arguments):
    result = subprocess.run([cli, *arguments], capture_output=True, encoding='utf-8',
                            errors='replace', timeout=120, startupinfo=hidden_startup(), cwd=Path.home())
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:400]
        raise RuntimeError(f'Codex 插件注册操作失败：{detail}')
    return json.loads(result.stdout)


def installed_entry(value: dict, destination: Path) -> dict | None:
    """Match the owned source directory before deciding whether a market is ambiguous."""
    entries = value.get('installed')
    if not isinstance(entries, list) or any(not isinstance(p, dict) for p in entries):
        raise RuntimeError('无法读取 Codex 插件注册列表，原安装未改变')
    product = [p for p in entries if p.get('name') == 'session-model-usage']
    matches = [p for p in product if registered_source_matches(p, destination)]
    if len(matches) > 1:
        raise RuntimeError(f'同一安装目录对应 {len(matches)} 个插件市场，未执行升级')
    if not matches:
        if product:
            raise RuntimeError('已注册插件目录与升级目标不一致，未执行升级')
        return None
    entry = matches[0]
    name = entry.get('marketplaceName')
    if not isinstance(name, str) or not name or entry.get('pluginId') != f'session-model-usage@{name}':
        raise RuntimeError('现有插件市场记录不完整，未执行升级')
    return entry


def registered_source_matches(entry: dict, destination: Path) -> bool:
    source = entry.get('source')
    if not isinstance(source, dict) or source.get('source', 'local') != 'local':
        return False
    path = source.get('path')
    return isinstance(path, str) and bool(path) and Path(path).is_absolute() and Path(path).resolve() == destination


def stop_installed(destination: Path):
    helper = destination / 'runtime/session-usage.exe'
    def command(action):
        result = subprocess.run([str(helper), action], capture_output=True, encoding='utf-8',
                                timeout=15, startupinfo=hidden_startup())
        return json.loads(result.stdout)
    before = command('status')
    if not before.get('running'):
        return
    command('stop')
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if not command('status').get('running'):
            return
        time.sleep(0.2)
    expected = (destination / 'runtime/CodexSessionUsage.exe').resolve()
    for pid_key, created_key in (('overlay_pid', 'overlay_created'), ('supervisor_pid', 'supervisor_created')):
        try:
            process = psutil.Process(before[pid_key])
            if abs(process.create_time() - before[created_key]) >= 0.1 or Path(process.exe()).resolve() != expected:
                raise RuntimeError('插件进程身份不匹配，未停止任何未知程序')
            process.terminate()
            process.wait(5)
        except (KeyError, psutil.NoSuchProcess):
            continue
    if command('status').get('running'):
        raise RuntimeError('插件仍在运行，未更改安装文件')


def copy_release(source: Path, target: Path):
    target.mkdir()
    for component in ('.codex-plugin', '.agents', 'skills', 'scripts', 'runtime', 'source', 'assets', 'docs', 'licenses'):
        item = source / component
        if item.exists():
            if item.is_symlink() or any(p.is_symlink() for p in item.rglob('*')):
                raise RuntimeError('安装包包含链接，已拒绝升级')
            shutil.copytree(item, target / component)
    for name in ('README.md', 'LICENSE', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md', 'Install.ps1', '安装插件.cmd', '启动悬浮条.cmd'):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)


def rename_installation(source: Path, target: Path):
    """Allow a bounded delay for Windows to release a just-checked executable."""
    for attempt in range(10):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


def upgrade(source: Path) -> dict:
    source = source.resolve()
    home = Path.home().resolve()
    if (home / 'plugins/session-model-usage').is_symlink():
        raise RuntimeError('升级目标是链接，未覆盖文件')
    destination = (home / 'plugins/session-model-usage').resolve()
    if destination.parent != (home / 'plugins').resolve() or destination.is_symlink():
        raise RuntimeError('升级目标不在个人插件目录')
    for relative in ('.codex-plugin/plugin.json', 'runtime/session-usage.exe', 'runtime/CodexSessionUsage.exe'):
        if not (source / relative).is_file():
            raise RuntimeError('升级包缺少程序或插件清单')
    manifest = json.loads((source / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
    if manifest.get('name') != 'session-model-usage' or source == destination:
        raise RuntimeError('升级包来源不正确')
    if not destination.is_dir():
        return install(source)
    old_manifest = json.loads((destination / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
    if old_manifest.get('name') != 'session-model-usage':
        raise RuntimeError('目标不是本产品，未覆盖文件')
    cli = find_cli()
    entry = installed_entry(cli_json(cli, 'plugin', 'list', '--json'), destination)
    pending_registration = entry is None
    if pending_registration:
        markets = cli_json(cli, 'plugin', 'marketplace', 'list', '--json').get('marketplaces')
        if not isinstance(markets, list) or any(not isinstance(p, dict) for p in markets):
            raise RuntimeError('无法读取 Codex 插件市场列表，原安装未改变')
        own_markets = [p for p in markets if p.get('name') == 'session-model-usage']
        if len(own_markets) > 1 or any(not isinstance(p.get('root'), str) or not Path(p['root']).is_absolute()
                                     or Path(p['root']).resolve() != destination for p in own_markets):
            raise RuntimeError('本插件市场已指向其他目录，未执行修复安装')
        entry = {'pluginId': 'session-model-usage@session-model-usage',
                 'marketplaceName': 'session-model-usage', 'enabled': True}
    if not entry.get('enabled', True):
        raise RuntimeError('原插件已禁用，未执行升级以保留设置')
    selector = entry['pluginId']
    backup_root = destination.parent / '.session-model-usage-backups'
    backup_root.mkdir(exist_ok=True)
    if backup_root.resolve().parent != destination.parent:
        raise RuntimeError('备份目录不在个人插件目录')
    token = str(int(time.time())) + '-' + uuid.uuid4().hex[:8]
    backup = backup_root / token
    staged = backup_root / (token + '-new')
    failed = backup_root / (token + '-failed')
    # Prepare and verify before touching the running installation.
    copy_release(source, staged)
    checked = subprocess.run([str(staged / 'runtime/session-usage.exe'), '--version'],
                             capture_output=True, text=True, timeout=15, startupinfo=hidden_startup())
    if checked.returncode or checked.stdout.strip() != manifest.get('version'):
        raise RuntimeError('升级程序版本与清单不一致，原安装未改变')
    stop_installed(destination)
    rename_installation(destination, backup)
    marketplace_added = False
    install_attempted = False
    try:
        rename_installation(staged, destination)
        if entry['marketplaceName'] == 'session-model-usage':
            write_local_marketplace(destination)
        if pending_registration:
            registration = cli_json(cli, 'plugin', 'marketplace', 'add', str(destination), '--json')
            marketplace_added = not registration.get('alreadyAdded', False)
        install_attempted = True
        cli_json(cli, 'plugin', 'add', selector, '--json')
        current = cli_json(cli, 'plugin', 'list', '--json').get('installed', [])
        if not any(p.get('pluginId') == selector and p.get('version') == manifest['version']
                   and registered_source_matches(p, destination) for p in current):
            raise RuntimeError('新插件缓存未生效')
    except Exception as error:
        if destination.exists():
            rename_installation(destination, failed)
        rename_installation(backup, destination)
        try:
            if pending_registration:
                if install_attempted:
                    cli_json(cli, 'plugin', 'remove', selector, '--json')
                if marketplace_added:
                    cli_json(cli, 'plugin', 'marketplace', 'remove', entry['marketplaceName'], '--json')
            else:
                cli_json(cli, 'plugin', 'add', selector, '--json')
        except Exception:
            record('upgrade_registration_rollback_failed')
        record('upgrade_rolled_back', error)
        raise RuntimeError('升级失败，已恢复原安装文件') from error
    shortcut = create_shortcut(destination) if pending_registration else None
    record('installation_repaired' if pending_registration else 'upgrade_completed', version=manifest['version'])
    result = {'status': 'repaired' if pending_registration else 'upgraded', 'version': manifest['version'], 'marketplace': entry['marketplaceName'],
            'backup': str(backup), 'source_path': str(destination), 'codex_interrupted': False,
            'message': '已修复安装。从开始菜单打开“Codex 会话用量”。' if pending_registration
                       else '已升级并保留快捷方式。打开现有启动器恢复悬浮条。'}
    if pending_registration:
        result['shortcut'] = str(shortcut) if shortcut else None
    return result
