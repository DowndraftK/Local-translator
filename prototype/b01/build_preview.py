"""Native development candidate build, explicitly separate from the old Swift App."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import plistlib
import py_compile
import shutil
import subprocess
import sys
import sysconfig
from datetime import datetime, timezone
from pathlib import Path


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    project = source.parents[1]
    lock_path = source / "dependencies.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(project / "dist"):
        parser.error("Use a NEW candidate directory under this repository's dist directory.")
    if not Path(sys.prefix).resolve().is_relative_to(project / "artifacts") or sys.prefix == sys.base_prefix:
        parser.error("Build using a separate project-local B01 development venv.")
    if platform.python_version() != "3.12.14":
        parser.error("Use pinned Python 3.12.14.")
    branch = run("git", "-C", project, "branch", "--show-current")
    if not branch.startswith("codex/"):
        parser.error("Build from the B01 continuation feature branch, not main.")
    for package in lock["packages"]:
        if importlib.metadata.version(package["name"]) != package["version"]:
            parser.error("Qt development dependency mismatch.")
    if sys.platform not in ("darwin", "win32"):
        parser.error("Build on native macOS/Windows only.")
    from PySide6.QtCore import qVersion
    if qVersion() != lock["qt"]:
        parser.error("Loaded Qt does not match lock.")
    output.mkdir(parents=True)
    mac_build = None
    if sys.platform == "darwin":
        existing = [46]
        for marker in (project / "dist").glob("*/build-number.txt"):
            try: existing.append(int(marker.read_text()))
            except (OSError, ValueError): pass
        for info in (project / "dist").glob("*/**/Contents/Info.plist"):
            try:
                data = plistlib.loads(info.read_bytes())
                if data.get("CFBundleIdentifier", "").startswith("local.kevin.translator"):
                    existing.append(int(data["CFBundleVersion"]))
            except (OSError, ValueError, KeyError): pass
        mac_build = max(existing) + 1
        (output / "build-number.txt").write_text(f"{mac_build}\n")
        build_id = f"macos-arm64-build{mac_build}"
        app = output / "本地翻译器 B01预览.app"
        package = app / "Contents/Resources/B01"
        (app / "Contents/MacOS").mkdir(parents=True)
    else:
        if platform.machine().lower() not in ("amd64", "x86_64"):
            parser.error("Requires Windows x64.")
        # Windows identity is independent of the historical Mac build sequence.
        build_id = "windows-x64-b01-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        package = output / "B01"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    identity = {"schema": 1, "product_version": "0.2.11", "preview_id": "b01-preview.1",
        "build_id": build_id, "mac_build": mac_build,
        "native_platform": sys.platform, "machine": platform.machine(),
        "built_at": datetime.now(timezone.utc).isoformat(), "branch": branch,
        "source_commit": run("git", "-C", project, "rev-parse", "HEAD"),
        "working_tree_modified": bool(run("git", "-C", project, "status", "--porcelain")),
        "source_files": {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "python_version": platform.python_version(), "python_executable": sys.executable,
        "qt_version": qVersion(), "protocol": {"major": 1, "minor": 0},
        "dependency_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "dependencies": {item["name"]: item["version"] for item in lock["packages"]},
        "standalone_runtime": False, "distribution": "local-development-preview-only"}
    (package / "build-identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in package.rglob("*.py"):
        target = output / "compile-check" / path.relative_to(package).with_suffix(".pyc")
        target.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(path), cfile=str(target), doraise=True)
    if sys.platform == "darwin":
        info = {"CFBundleIdentifier": "local.kevin.translator.b01", "CFBundleName": "本地翻译器 B01预览",
            "CFBundleDisplayName": "本地翻译器 B01预览", "CFBundleExecutable": "B01Preview",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.2.11",
            "CFBundleVersion": str(mac_build), "LSMinimumSystemVersion": "26.0",
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        executable = app / "Contents/MacOS/B01Preview"
        subprocess.run(["clang", "-Os", "-arch", "arm64", '-DB01_PYTHON_PATH=' + json.dumps(sys.executable),
                        "-I", sysconfig.get_path("include"), str(source / "native/mac_launcher.c"),
                        str(Path(sys.base_prefix) / "lib/libpython3.12.dylib"),
                        "-Wl,-rpath," + str(Path(sys.base_prefix) / "lib"), "-o", str(executable)], check=True)
        subprocess.run(["codesign", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    else:
        # No installer or claimed independent EXE in B01: native Python/Qt bytecode
        # build plus an explicit local interpreter launcher for desktop verification.
        launcher = f'@echo off\r\n"{sys.executable}" -I -B "%~dp0B01\\launch.py" %*\r\n'
        (output / "Launch-B01.cmd").write_text(launcher, encoding="utf-8")
    (output / "candidate.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"build_id": build_id, "output": str(output), "source_files": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
