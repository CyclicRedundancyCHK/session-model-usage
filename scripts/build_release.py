"""Build a clean Windows distribution from an explicit public file allowlist."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("source", "scripts", "skills", "licenses", "assets", "docs", ".codex-plugin", ".agents")
FILES = ("README.md", "LICENSE", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "Install.ps1",
         "安装插件.cmd", "启动悬浮条.cmd")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I)
USER_PATH = re.compile(r"[A-Za-z]:[/\\]Users[/\\][A-Za-z0-9_. -]+[/\\]", re.I)
TOKEN = re.compile(r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{24,})")
TEXT_SUFFIXES = {".py", ".cs", ".md", ".json", ".toml", ".yml", ".yaml", ".ps1", ".cmd", ".spec", ".manifest", ".txt"}


def inspect_public_file(path: Path) -> None:
    if path.is_symlink() or path.name.lower() in ("auth.json", "credentials.json", "hosts.yml", ".env"):
        raise ValueError(f"Private or linked file refused: {path.name}")
    if path.suffix.lower() in (".sqlite", ".jsonl", ".log", ".pyc"):
        raise ValueError(f"Private/generated file refused: {path.name}")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return
    value = path.read_text(encoding="utf-8-sig")
    if USER_PATH.search(value) or TOKEN.search(value):
        raise ValueError(f"Personal path or credential-like text refused: {path.name}")
    for match in UUID.finditer(value):
        if not match.group().startswith("00000000-"):
            raise ValueError(f"Non-example conversation identifier refused: {path.name}")


def public_files(root: Path) -> list[Path]:
    paths = [root/name for name in FILES]
    for folder in FOLDERS:
        paths.extend(p for p in (root/folder).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc")
    for path in paths:
        assert path.resolve().is_relative_to(root.resolve()), path.name
        inspect_public_file(path)
    return sorted(paths)


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.platform != "win32":
        raise RuntimeError("Build the Windows binaries on Windows x64")
    version = json.loads((ROOT/".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    paths = public_files(ROOT)
    build = ROOT/".build"
    build.mkdir(exist_ok=True)
    environment = {**os.environ, "PYTHONPATH": str(ROOT/"source"), "PYTHONUTF8": "1"}
    tests = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover",
                            "-s", str(ROOT/"source/tests"), "-v"], env=environment,
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    test_output = tests.stdout + tests.stderr
    (build/"tests.log").write_text(test_output, encoding="utf-8")
    if tests.returncode:
        print(test_output)
        return tests.returncode
    count = int(re.search(r"Ran (\d+) tests", test_output).group(1))
    print(f"Public source audit passed; {count} tests passed", flush=True)
    from build_quota import build as build_native
    native_tray = build_native()
    with (build/"build.log").open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(build/"dist"),
                        "--workpath", str(build/"work"), str(ROOT/"source/build.spec")],
                        stdout=log, stderr=subprocess.STDOUT, check=True, env=environment, cwd=ROOT)
    runtime = build/"dist/runtime"
    shutil.copy2(native_tray, runtime / native_tray.name)
    if any("plugin_tools" in p.parts for p in runtime.rglob("*")):
        raise RuntimeError("Development scaffolding must not be bundled")
    staging = build/"release/session-model-usage"
    if staging.exists():
        # Preserve the previous build, but never carry removed/renamed sources
        # into the next installation directory.
        prior = build/"release/history"/f"session-model-usage-{time.time_ns()}"
        if staging.is_symlink() or not staging.resolve().is_relative_to(build.resolve()) or not prior.resolve().is_relative_to(build.resolve()):
            raise RuntimeError("Release staging paths must stay within the build directory")
        prior.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(prior)
    staging.mkdir(parents=True, exist_ok=True)
    staged = []
    for path in paths:
        destination = staging/path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        staged.append(destination)
    for path in runtime.rglob("*"):
        if path.is_file():
            destination = staging/"runtime"/path.relative_to(runtime)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            staged.append(destination)
    validation = staging/"validation-summary.json"
    native_validation = json.loads((build/"quota/test-summary.json").read_text(encoding="utf-8"))
    validation.write_text(json.dumps({"version":version,"platform":"Windows x64",
        "python":platform.python_version(),"offline_tests_passed":count,
        "native_checks":native_validation,
        "public_file_audit":"passed","no_validation_model_requests":True,
        "real_user_logs_included":False,"private_test_profiles_included":False},indent=2)+"\n",encoding="utf-8")
    staged.append(validation)
    sums = staging/"SHA256SUMS.txt"
    sums.write_text("\n".join(f"{checksum(p)}  {p.relative_to(staging).as_posix()}" for p in sorted(staged))+"\n",encoding="utf-8")
    staged.append(sums)
    dist = ROOT/"dist"
    dist.mkdir(exist_ok=True)
    archive = dist/f"session-model-usage-v{version}-windows-x64.zip"
    with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for path in sorted(staged):
            bundle.write(path, "session-model-usage/"+path.relative_to(staging).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed")
        assert not any("plugin_tools" in name or "/validation/" in name for name in bundle.namelist())
    digest = checksum(archive)
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n",encoding="utf-8")
    print(json.dumps({"archive":str(archive),"sha256":digest,"bytes":archive.stat().st_size,
                      "files":len(staged),"tests":count},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
