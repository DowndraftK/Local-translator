"""Fetch only the pinned Qt development wheels into a new project-local venv."""
import argparse
import hashlib
import json
import platform
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--wheel-directory", type=Path,
                        help="Use predownloaded fixed wheels; still validate every byte before creating the venv")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    destination = args.environment.resolve()
    if not destination.is_relative_to(project / "artifacts") or destination.exists():
        parser.error("Use a NEW environment inside this repository's artifacts directory.")
    lock = json.loads(Path(__file__).with_name("dependencies.lock.json").read_text(encoding="utf-8"))
    if platform.python_version() != lock["python"]["version"] or sys.platform not in ("darwin", "win32"):
        parser.error("Requires native macOS/Windows and the pinned Python 3.12.14 development interpreter.")
    if sys.platform == "win32" and platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error("Requires native Windows x64, not WSL/ARM.")
    wheels = args.wheel_directory.resolve() if args.wheel_directory else destination.parent / (destination.name + "-wheels")
    if not wheels.is_relative_to(project / "artifacts"):
        parser.error("Development wheel cache must be under this repository's artifacts directory.")
    if not args.wheel_directory: wheels.mkdir(exist_ok=False)
    downloaded = []
    for package in lock["packages"]:
        item = package["wheels"][sys.platform]
        target = wheels / item["filename"]
        digest = hashlib.sha256(); count = 0
        if args.wheel_directory:
            with target.open("rb") as cached:
                while chunk := cached.read(1024 * 1024):
                    count += len(chunk); digest.update(chunk)
                    if count > item["bytes"]: raise ValueError("oversized cached development wheel")
        else:
            with urllib.request.urlopen(item["url"], timeout=30) as response, target.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > item["bytes"]:
                        raise ValueError("oversized development wheel")
                    digest.update(chunk); output.write(chunk)
        if count != item["bytes"] or digest.hexdigest() != item["sha256"]:
            raise ValueError("development wheel identity mismatch; nothing installed")
        downloaded.append(target)
    venv.EnvBuilder(with_pip=True, symlinks=False).create(destination)
    python = destination / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps",
                    "--no-compile", "--no-cache-dir", "--disable-pip-version-check",
                    *map(str, downloaded)], check=True)
    print(f"Prepared only this isolated development environment: {destination}")


if __name__ == "__main__":
    main()
