#!/bin/sh
set -eu

# One-command, per-user source installer. It never invokes sudo or a system
# package manager; GTK/PyGObject must already be available from the OS.
exec /usr/bin/python3 - "$@" <<'PY'
from __future__ import annotations

import io
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

REPOSITORY_ARCHIVE = "https://api.github.com/repos/Dev-in-the-BM/Fuse/tarball/main"
APP_FILES = (
    "app_settings.py", "backend_manager.py", "bounded_logging.py",
    "mtk_adwaita.py", "mtk_image_identify.py", "sitecustomize.py",
    "run.sh", "install.sh", "install-desktop-entry.py", "LICENSE",
)
RUNTIME_ASSETS = (
    "52-mtkclient.rules", "connect-power-off.svg", "connect-test-point.svg",
    "connect-usb-buttons.svg", "connect-usb-preloader.svg",
    "device-connected.svg", "device-ready.svg", "fuse-mtk-app-icon-128.png",
    "reboot-device.png",
)
DESKTOP_ICONS = tuple(
    f"fuse-mtk-app-icon-{size}.png"
    for size in (16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512)
) + ("fuse-mtk-app-icon.svg",)


def check_runtime() -> None:
    if sys.version_info < (3, 10):
        print("Fuse requires Python 3.10 or newer.", file=sys.stderr)
        raise SystemExit(1)
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        import cairo  # noqa: F401
    except Exception as exc:
        print(f"Fuse's system GTK/Python runtime is missing: {exc}", file=sys.stderr)
        print("Install your distribution's Python 3, PyGObject, GTK 4, libadwaita, and Cairo runtime packages, then rerun this command.", file=sys.stderr)
        raise SystemExit(1)
    if not hasattr(Adw, "ToolbarView"):
        print("Fuse requires libadwaita 1.4 or newer (Adw.ToolbarView is unavailable).", file=sys.stderr)
        raise SystemExit(1)


def unpack_source(archive: bytes, destination: Path) -> str:
    wanted = {PurePosixPath(name) for name in APP_FILES}
    wanted.update(PurePosixPath("assets") / name for name in RUNTIME_ASSETS + DESKTOP_ICONS)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        roots = {PurePosixPath(member.name).parts[0] for member in members
                 if PurePosixPath(member.name).parts}
        if len(roots) != 1:
            raise RuntimeError("Unexpected source archive layout")
        root = roots.pop()
        for member in members:
            path = PurePosixPath(member.name)
            if not path.parts or path.parts[0] != root or ".." in path.parts:
                raise RuntimeError("Unsafe path in source archive")
            relative = path.parts[1:]
            relative_path = PurePosixPath(*relative) if relative else PurePosixPath()
            if not relative or relative_path not in wanted:
                continue
            if not member.isfile():
                raise RuntimeError("Expected source file is missing or is not a regular file")
            target = destination.joinpath(*relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"Cannot read archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(stat.S_IMODE(member.mode) & 0o755)
        missing = [str(path) for path in wanted
                   if not destination.joinpath(*path.parts).is_file()]
        if missing:
            raise RuntimeError("Source archive is missing required files: " + ", ".join(missing))
    return root


def copy_worktree(source: Path, destination: Path) -> None:
    files = (*APP_FILES, *(f"assets/{name}" for name in RUNTIME_ASSETS + DESKTOP_ICONS))
    for name in files:
        source_file = source / name
        if not source_file.is_file():
            raise RuntimeError(f"Source checkout is missing {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)


def source_revision(source: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(source).as_posix().encode())
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def main() -> int:
    check_runtime()
    install_root = Path.home() / ".local/opt/fuse"
    install_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fuse-install-", dir=install_root) as temporary:
        staging = Path(temporary) / "source"
        staging.mkdir()
        worktree = Path.cwd()
        if all((worktree / name).is_file() for name in ("mtk_adwaita.py", "run.sh", "install-desktop-entry.py")):
            print("Installing this source tree…", flush=True)
            copy_worktree(worktree, staging)
        else:
            request = Request(REPOSITORY_ARCHIVE, headers={"User-Agent": "Fuse-source-installer"})
            print("Downloading Fuse source from GitHub…", flush=True)
            with urlopen(request, timeout=60) as response:
                archive = response.read()
            unpack_source(archive, staging)
        revision = source_revision(staging)[:16]
        installed = install_root / f"source-{revision}"
        if not installed.exists():
            os.replace(staging, installed)

    launcher = installed / "run.sh"
    if not launcher.is_file():
        raise RuntimeError("Downloaded source is missing run.sh")
    launcher.chmod(0o755)
    desktop_installer = installed / "install-desktop-entry.py"
    if not desktop_installer.is_file():
        raise RuntimeError("Downloaded source is missing its app-grid installer")
    os.execv("/usr/bin/python3", ["/usr/bin/python3", str(desktop_installer)])


try:
    raise SystemExit(main())
except Exception as exc:
    print(f"Fuse source installation failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
