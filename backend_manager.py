"""Download and prepare a private MTK Client backend without root privileges."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import shlex
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

BACKEND_ARCHIVE_URL = "https://api.github.com/repos/bkerler/mtkclient/tarball"
APP_DIR = Path(__file__).resolve().parent


def default_backend_dir():
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "fuse" / "backend"


def is_backend_checkout(path):
    path = Path(path).expanduser()
    return (path / "pyproject.toml").is_file() and (path / "uv.lock").is_file()


def backend_python_paths(checkout):
    """Return backend source and its installed venv packages, if present."""
    checkout = Path(checkout).expanduser().resolve()
    site_packages = sorted((checkout / ".venv" / "lib").glob("python*/site-packages"))
    return [checkout, *site_packages]


def install_backend(destination=None, progress=None, log=None):
    """Download upstream and install its CLI requirements without Qt; return path."""
    destination = Path(destination or default_backend_dir()).expanduser()
    if (destination / "pyproject.toml").is_file() and (destination / "uv.lock").is_file():
        checkout = destination
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Install location is not empty: {destination}")
        if progress:
            progress("Downloading MTK Client…")
        _download_backend(destination, log=log)
        checkout = destination

    if not (checkout / "pyproject.toml").is_file():
        raise RuntimeError("Downloaded checkout is missing pyproject.toml")
    requirements = checkout / "requirements.txt"
    if not requirements.is_file():
        raise RuntimeError("Downloaded checkout is missing requirements.txt")
    if progress:
        progress("Installing backend dependencies…")
    venv = checkout / ".venv"
    python = os.environ.get("PYTHON") or getattr(sys, "_base_executable", sys.executable)
    uv = shutil.which("uv")
    # Reset the app-managed environment so a previous install cannot retain
    # PySide6 (or its large Qt wheels) after switching to the filtered list.
    if uv:
        _run([uv, "venv", "--clear", "--python", python, str(venv)], log=log)
    else:
        _run([python, "-m", "venv", "--clear", str(venv)], log=log)
    filtered_requirements = filter_cli_requirements(requirements.read_text(encoding="utf-8"))
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix="-requirements.txt") as stream:
        stream.write(filtered_requirements)
        stream.flush()
        if uv:
            command = [
                uv, "pip", "install", "--python", str(venv / "bin" / "python"),
                "--requirements", stream.name,
            ]
        else:
            command = [
                str(venv / "bin" / "python"), "-m", "pip", "install",
                "--disable-pip-version-check", "--requirement", stream.name,
            ]
        _run(command, cwd=checkout, log=log)
    return str(checkout.resolve())


def filter_cli_requirements(requirements_text):
    """Keep upstream requirements except the separate PySide6 GUI stack."""
    kept = []
    for line in requirements_text.splitlines():
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            package = match.group(1).lower().replace("_", "-").replace(".", "-")
            if package.startswith("pyside6") or package == "shiboken6":
                continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def _download_backend(destination, log=None):
    """Download and safely unpack the upstream source without requiring git."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        BACKEND_ARCHIVE_URL,
        headers={"User-Agent": "Fuse-MTK-Client"},
    )
    with tempfile.TemporaryDirectory(
        prefix="fuse-backend-", dir=destination.parent
    ) as temporary:
        temporary = Path(temporary)
        archive_path = temporary / "backend.tar.gz"
        with urlopen(request, timeout=60) as response, archive_path.open("wb") as stream:
            shutil.copyfileobj(response, stream)

        staging = temporary / "checkout"
        staging.mkdir()
        _extract_backend_archive(archive_path, staging)
        if not (staging / "pyproject.toml").is_file() or not (staging / "uv.lock").is_file():
            raise RuntimeError("Downloaded checkout is missing pyproject.toml or uv.lock")
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Install location is not empty: {destination}")
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
    if log:
        log("Downloaded MTK Client source archive.")


def _extract_backend_archive(archive_path, destination):
    """Extract regular archive files while rejecting path escapes and links."""
    destination = Path(destination).resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if len(roots) != 1:
            raise RuntimeError("Downloaded backend archive has an unexpected layout")
        archive_root = roots.pop()
        for member in members:
            parts = Path(member.name).parts
            if not parts or parts[0] != archive_root or ".." in parts or member.issym() or member.islnk():
                raise RuntimeError("Downloaded backend archive contains an unsafe path")
            relative_parts = parts[1:]
            if not relative_parts:
                continue
            target = destination.joinpath(*relative_parts)
            if os.path.commonpath((str(destination), str(target.resolve()))) != str(destination):
                raise RuntimeError("Downloaded backend archive contains an unsafe path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("Could not read a file in the backend archive")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def _run(command, cwd=None, log=None):
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in process.stdout:
        if log:
            log(line.rstrip())
    result = process.wait()
    if result:
        raise subprocess.CalledProcessError(result, command)


def setup_usb_permissions(rule_file=None):
    """Install the focused udev rule via PolicyKit; caller checks availability."""
    pkexec = shutil.which("pkexec")
    install = shutil.which("install")
    udevadm = shutil.which("udevadm")
    if not all((pkexec, install, udevadm)):
        raise RuntimeError("PolicyKit (pkexec), install, or udevadm is unavailable")
    rule_file = Path(rule_file or APP_DIR / "assets/52-mtkclient.rules").resolve()
    command = (
        f"{shlex.quote(install)} -m 644 {shlex.quote(str(rule_file))} "
        f"/etc/udev/rules.d/52-mtkclient.rules && "
        f"{shlex.quote(udevadm)} control --reload-rules && "
        f"{shlex.quote(udevadm)} trigger --subsystem-match=usb"
    )
    subprocess.run([pkexec, "/bin/sh", "-c", command], check=True)
