#!/usr/bin/env python3
"""Stage the current Fuse worktree (including edits) for a Flatpak build."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend_manager import filter_cli_requirements


MANIFEST = ROOT / "packaging/flatpak/manifest.json"
LIBFUSE2_URL = (
    "https://archive.ubuntu.com/ubuntu/pool/universe/f/fuse/"
    "libfuse2_2.9.9-5ubuntu3_amd64.deb"
)
LIBFUSE2_SHA256 = "accb0569b063a9d21e2c2a4e30b299da4e87d11af3b41ac1c01f7329ba393ea2"
APP_FILES = (
    "app_settings.py",
    "backend_manager.py",
    "bounded_logging.py",
    "mtk_adwaita.py",
    "mtk_image_identify.py",
    "sitecustomize.py",
)
BACKEND_IGNORES = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache"
)


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage(source: Path) -> str:
    for filename in APP_FILES:
        copy_file(ROOT / filename, source / filename)
    for asset in runtime_assets():
        copy_file(ROOT / asset, source / asset)
    shutil.copytree(ROOT / "mtkclient", source / "mtkclient", dirs_exist_ok=True,
                    ignore=BACKEND_IGNORES)
    copy_file(ROOT / "debian/quest.devinthebm.fuse.desktop",
              source / "quest.devinthebm.fuse.desktop")
    copy_file(ROOT / "assets/fuse-mtk-app-icon.svg",
              source / "quest.devinthebm.fuse.svg")
    copy_file(ROOT / "packaging/flatpak/fuse", source / "fuse")
    stage_libfuse2(source)

    digest = hashlib.sha256()
    for path in sorted((source / "mtkclient").rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(source / "mtkclient").as_posix().encode())
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    revision = digest.hexdigest()
    (source / "backend-revision").write_text(revision + "\n", encoding="ascii")
    return revision


def runtime_assets() -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in (ROOT / "packaging/runtime-assets.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def stage_libfuse2(source: Path) -> None:
    """Bundle Ubuntu 22.04's old-ABI libfuse for mfusepy's optional mount API."""
    package = source.parent / "libfuse2.deb"
    with urlopen(LIBFUSE2_URL, timeout=60) as response, package.open("wb") as output:
        shutil.copyfileobj(response, output)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    if digest != LIBFUSE2_SHA256:
        package.unlink(missing_ok=True)
        raise RuntimeError(f"Unexpected libfuse2 package checksum: {digest}")
    extracted = source.parent / "libfuse2"
    if extracted.exists():
        shutil.rmtree(extracted)
    subprocess.run(["dpkg-deb", "-x", str(package), str(extracted)], check=True)
    library = extracted / "lib/x86_64-linux-gnu/libfuse.so.2.9.9"
    if not library.is_file():
        raise RuntimeError("libfuse2 package does not contain libfuse.so.2.9.9")
    copy_file(library, source / "libfuse.so.2")


def prepare_dependencies(source: Path, build_root: Path) -> None:
    requirements = build_root / "requirements.txt"
    requirements.write_text(
        filter_cli_requirements(
            (source / "mtkclient/requirements.txt").read_text(encoding="utf-8")
        ),
        encoding="utf-8",
    )
    subprocess.run([
        "uv", "pip", "install", "--offline", "--no-config", "--link-mode=copy",
        "--target", str(source / "python-libs"),
        "--python-version", "3.14",
        "--python-platform", "x86_64-manylinux_2_34",
        "--requirement", str(requirements),
    ], check=True, cwd=ROOT)


def main() -> int:
    persistent_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    default_build_root = persistent_data / "fuse" / "flatpak-build"
    build_root = Path(os.environ.get("FUSE_FLATPAK_BUILD_DIR", default_build_root))
    build_root.mkdir(parents=True, exist_ok=True)
    source = build_root / "source"
    if source.exists():
        shutil.rmtree(source)
    source.mkdir()
    revision = stage(source)
    prepare_dependencies(source, build_root)

    manifest = build_root / "manifest.json"
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
    repo = build_root / "repo"
    build_dir = build_root / "build"
    subprocess.run([
        "flatpak-builder", "--force-clean", f"--state-dir={build_root / 'state'}",
        f"--repo={repo}", str(build_dir), str(manifest)
    ], check=True)

    version = subprocess.check_output(
        ["dpkg-parsechangelog", "-S", "Version"], cwd=ROOT, text=True
    ).strip()
    output = ROOT.parent / f"quest.devinthebm.fuse_{version}_x86_64.flatpak"
    subprocess.run([
        "flatpak", "build-bundle", str(repo), str(output),
        "quest.devinthebm.fuse", "master",
    ], check=True)
    print(f"Created {output} (bundled backend revision {revision[:12]}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
