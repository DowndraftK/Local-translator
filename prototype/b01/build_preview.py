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
    parser.add_argument("--pdf-library", type=Path, help="Pinned wheel unpacked under a NEW artifacts directory on Windows")
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
    sys.path[:0] = [str(source), str(project / "shared")]
    from translator_preview import PREVIEW_ID
    from translator_preview.runtime import owned_python
    from translator_core import CORE_VERSION
    from translator_preview.protocol import PROTOCOL
    stage = PREVIEW_ID.split("-", 1)[0].upper()
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
        app = output / f"本地翻译器 {stage}预览.app"
        package = app / "Contents/Resources/B01"
        (app / "Contents/MacOS").mkdir(parents=True)
    else:
        if platform.machine().lower() not in ("amd64", "x86_64"):
            parser.error("Requires Windows x64.")
        # Windows identity is independent of the historical Mac build sequence.
        build_id = "windows-x64-" + stage.lower() + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        package = output / "B01"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(project / "shared", package / "shared", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assets = package / "shared/translator_core/assets"; assets.mkdir()
    for recipe in (project / "fixtures/b01").glob("swift-recipe-*.json"):
        shutil.copy2(recipe, assets / recipe.name)
    if sys.platform == "darwin":
        helper = package / "shared/platform/macos/text-boundaries"
        subprocess.run(["swiftc", "-O", "-module-cache-path", str(output / "module-cache"),
                        str(project / "shared/platform/macos/TextBoundaries.swift"), "-o",
                        str(helper)], check=True)
        subprocess.run(["codesign", "--sign", "-", str(helper)], check=True)
        pdf_helper = package / "shared/platform/macos/pdf-facts"
        subprocess.run(["swiftc", "-O", "-module-cache-path", str(output / "module-cache"),
                        str(project / "shared/platform/macos/PDFFacts.swift"), "-o", str(pdf_helper)], check=True)
        subprocess.run(["codesign", "--sign", "-", str(pdf_helper)], check=True)
    elif args.pdf_library:
        library = args.pdf_library.resolve()
        if not library.is_relative_to(project / "artifacts"):
            parser.error("Use the fixed PDF software library under this project artifacts root.")
        pdf_lock = json.loads((source / "pdf-dependencies.lock.json").read_text(encoding="utf-8"))
        package_lock = pdf_lock["packages"][0]
        wheel = library.parent / package_lock["filename"]
        if wheel.stat().st_size != package_lock["bytes"] or hashlib.sha256(wheel.read_bytes()).hexdigest() != package_lock["sha256"]:
            parser.error("Official PDF wheel hash/size mismatch.")
        import zipfile
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.endswith("/"): continue
                path = library / name
                if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(archive.read(name)).digest():
                    parser.error("Unpacked PDF library differs from the official locked wheel.")
        shutil.copytree(library, package / "vendor/pdfium", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = sorted(p for root in (source, project / "shared") for p in root.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    files += sorted((project / "fixtures/b01").glob("swift-recipe-*.json"))
    identity = {"schema": 1, "product_version": "0.2.11", "preview_id": PREVIEW_ID,
        "build_id": build_id, "mac_build": mac_build,
        "native_platform": sys.platform, "machine": platform.machine(),
        "built_at": datetime.now(timezone.utc).isoformat(), "branch": branch,
        "source_commit": run("git", "-C", project, "rev-parse", "HEAD"),
        "working_tree_modified": bool(run("git", "-C", project, "status", "--porcelain")),
        "source_files": {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "python_version": platform.python_version(), "python_executable": sys.executable,
        "qt_version": qVersion(), "protocol": PROTOCOL, "shared_core": CORE_VERSION,
        "worker_executable": owned_python(),
        "preview_task_root": str(project / f"artifacts/{stage.lower()}-20261005/tasks"),
        "dependency_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "dependencies": {item["name"]: item["version"] for item in lock["packages"]},
        "standalone_runtime": False, "distribution": "local-development-preview-only"}
    if sys.platform == "darwin":
        identity["platform_adapter"] = {"name": "Foundation-Character-NaturalLanguage-boundaries-v1",
                                       "binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                                       "compiler": run("swiftc", "--version")}
    else:
        identity["platform_adapter"] = {"name": "Qt-QTextBoundaryFinder-6.10.2",
                                       "splitter_version": "qt6102-unicode-utf8-v1"}
    from translator_core.pdf_adapter import platform_pdf_adapter
    pdf_probe = platform_pdf_adapter(output / "fact-probe", pdf_helper if sys.platform == "darwin" else None,
                                     owned_python(), package / "vendor/pdfium" if sys.platform == "win32" else None)
    identity["pdf_adapter"] = {"identity": pdf_probe.identity, "actual_probe_confirmed": pdf_probe.verified,
                               "render_max_edge": 3000, "render_max_pixels": 9000000,
                               "library_lock_sha256": hashlib.sha256((source / "pdf-dependencies.lock.json").read_bytes()).hexdigest()}
    from translator_core.ocr_adapter import platform_ocr_adapter
    ocr_probe = platform_ocr_adapter(output / 'ocr-probe', pdf_probe, pdf_helper if sys.platform=='darwin' else None)
    identity['ocr_adapter'] = dict(identity=ocr_probe.identity,actual_probe_confirmed=ocr_probe.verified,
                                   available_languages=ocr_probe.languages,profile=ocr_probe.probe['profile'])
    (package / "build-identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in package.rglob("*.py"):
        target = output / "compile-check" / path.relative_to(package).with_suffix(".pyc")
        target.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(path), cfile=str(target), doraise=True)
    if sys.platform == "darwin":
        info = {"CFBundleIdentifier": "local.kevin.translator." + stage.lower(), "CFBundleName": f"本地翻译器 {stage}预览",
            "CFBundleDisplayName": f"本地翻译器 {stage}预览", "CFBundleExecutable": "B02Preview",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.2.11",
            "CFBundleVersion": str(mac_build), "LSMinimumSystemVersion": "26.0",
            "NSDocumentsFolderUsageDescription": "此开发预览需要读取文稿目录中的项目运行环境，并保存独立的测试任务。",
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        executable = app / "Contents/MacOS/B02Preview"
        subprocess.run(["clang", "-Os", "-arch", "arm64", '-DB01_PYTHON_PATH=' + json.dumps(sys.executable),
                        "-I", sysconfig.get_path("include"), str(source / "native/mac_launcher.c"),
                        str(Path(sys.base_prefix) / "lib/libpython3.12.dylib"),
                        "-Wl,-rpath," + str(Path(sys.base_prefix) / "lib"), "-o", str(executable)], check=True)
        subprocess.run(["codesign", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    else:
        # No installer or claimed independent EXE in B01: native Python/Qt bytecode
        # build plus an explicit local interpreter launcher for desktop verification.
        # Keep cmd's bytes ASCII/CRLF. Environment variables carry Unicode paths
        # through a companion PowerShell file, outside batch source parsing.
        ps = "$ErrorActionPreference='Stop'\n$env:__PYVENV_LAUNCHER__='" + str(sys.executable).replace("'", "''") + "'\n& '" + owned_python().replace("'", "''") + "' -I -B -X utf8 (Join-Path $PSScriptRoot 'B01\\launch.py') @args\nexit $LASTEXITCODE\n"
        (output / "Launch-Preview.ps1").write_bytes(b'\xef\xbb\xbf' + ps.encode('utf-8'))
        launcher = '@echo off\r\nsetlocal\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-Preview.ps1" %*\r\nexit /b %errorlevel%\r\n'
        (output / f"Launch-{stage}.cmd").write_bytes(launcher.encode("ascii"))
    (output / "candidate.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"build_id": build_id, "output": str(output), "source_files": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
