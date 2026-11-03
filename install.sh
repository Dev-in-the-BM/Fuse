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
import subprocess
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
RUNTIME_PROBE = """
import sys
if sys.version_info < (3, 10):
    print(f"Python 3.10 or newer is required (found {sys.version.split()[0]}).")
    raise SystemExit(1)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    import cairo
except Exception as exc:
    print(f"GTK/Python runtime is unavailable: {exc}")
    raise SystemExit(1)
if not hasattr(Adw, "ToolbarView"):
    print("libadwaita 1.4 or newer is required (Adw.ToolbarView is unavailable).")
    raise SystemExit(1)
"""

DEPENDENCIES = {
    "apt": (
        "python3", "python3-gi", "python3-gi-cairo", "python3-cairo",
        "gir1.2-gtk-4.0", "gir1.2-adw-1",
    ),
    "dnf": (
        "python3", "python3-gobject", "python3-cairo", "gtk4", "libadwaita",
    ),
    "zypper": (
        "python3", "python3-gobject", "python3-cairo", "gtk4", "libadwaita",
        "typelib-1_0-Gtk-4_0", "typelib-1_0-Adw-1",
    ),
    "pacman": (
        "python", "python-gobject", "python-cairo", "gtk4", "libadwaita",
    ),
    "apk": (
        "python3", "py3-gobject3", "py3-cairo", "gtk4.0", "libadwaita",
    ),
}


def runtime_problems() -> list[str]:
    result = subprocess.run(
        ["/usr/bin/python3", "-c", RUNTIME_PROBE], capture_output=True,
        text=True, check=False,
    )
    if result.returncode == 0:
        return []
    diagnostics = result.stderr + result.stdout
    return [line for line in diagnostics.splitlines() if line.strip()] or [
        "Python/GTK runtime check failed"
    ]


def detect_package_manager() -> str | None:
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"').lower()
    except OSError:
        pass
    ids = {os_release.get("id", "")}
    ids.update(os_release.get("id_like", "").split())
    families = (
        ("apt", {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "raspbian", "kali", "deepin", "mx"}),
        ("dnf", {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn", "nobara"}),
        ("zypper", {"suse", "opensuse", "opensuse-leap", "opensuse-tumbleweed", "sled", "sles"}),
        ("pacman", {"arch", "archarm", "manjaro", "endeavouros", "artix", "cachyos", "garuda", "steamos"}),
        ("apk", {"alpine"}),
    )
    executables = {"apt": "apt-get", "dnf": "dnf", "zypper": "zypper",
                   "pacman": "pacman", "apk": "apk"}
    for manager, family_ids in families:
        if ids & family_ids and shutil.which(executables[manager]):
            return manager
    for manager, executable in executables.items():
        if shutil.which(executable):
            return manager
    return None


def consent_to_dependencies(manager: str, problems: list[str]) -> bool:
    if os.geteuid() == 0:
        print("Run install.sh as your normal user; it will request administrator authorization when needed.", file=sys.stderr)
        return False
    privilege = shutil.which("sudo") or shutil.which("doas")
    if not privilege:
        print("Missing system dependencies, but neither sudo nor doas is available to install them.", file=sys.stderr)
        return False
    packages = DEPENDENCIES[manager]
    prefix = Path(privilege).name
    commands = {
        "apt": f"{prefix} apt-get update && {prefix} apt-get install -y {' '.join(packages)}",
        "dnf": f"{prefix} dnf install -y {' '.join(packages)}",
        "zypper": f"{prefix} zypper --non-interactive install {' '.join(packages)}",
        "pacman": f"{prefix} pacman -S --needed --noconfirm {' '.join(packages)}",
        "apk": f"{prefix} apk add --no-cache {' '.join(packages)}",
    }
    print("Fuse needs these system dependencies:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(f"Detected package manager: {manager}", file=sys.stderr)
    print(f"Packages: {' '.join(packages)}", file=sys.stderr)
    print(f"Command (after your approval): {commands[manager]}", file=sys.stderr)
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        print("Cannot prompt without a terminal; rerun this installer interactively to approve dependency installation.", file=sys.stderr)
        return False
    with tty:
        tty.write("Install these dependencies with your system package manager? [y/N] ")
        tty.flush()
        return tty.readline().strip().lower() in ("y", "yes")


def install_dependencies(manager: str) -> None:
    privilege = shutil.which("sudo") or shutil.which("doas")
    if not privilege:
        raise RuntimeError("Dependency installation needs sudo or doas, but neither is available.")
    packages = DEPENDENCIES[manager]
    commands = {
        "apt": [["apt-get", "update"], ["apt-get", "install", "-y", *packages]],
        "dnf": [["dnf", "install", "-y", *packages]],
        "zypper": [["zypper", "--non-interactive", "install", *packages]],
        "pacman": [["pacman", "-S", "--needed", "--noconfirm", *packages]],
        "apk": [["apk", "add", "--no-cache", *packages]],
    }
    for command in commands[manager]:
        subprocess.run([privilege, *command], check=True)


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
    problems = runtime_problems()
    if problems:
        manager = detect_package_manager()
        if not manager:
            print("Could not identify a supported package manager. Supported families are Debian/Ubuntu, Fedora/RHEL, openSUSE, Arch, and Alpine.", file=sys.stderr)
            print("Missing runtime: " + "; ".join(problems), file=sys.stderr)
            raise SystemExit(1)
        if not consent_to_dependencies(manager, problems):
            print("Dependency installation was not approved; Fuse was not installed.", file=sys.stderr)
            raise SystemExit(1)
        install_dependencies(manager)
        problems = runtime_problems()
        if problems:
            print("The package manager finished, but Fuse's runtime is still insufficient:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("Your distribution may be too old to provide libadwaita 1.4+; upgrade it or install newer GTK/libadwaita packages, then rerun install.sh.", file=sys.stderr)
            raise SystemExit(1)
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
