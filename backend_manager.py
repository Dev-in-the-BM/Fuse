"""Download and prepare a private MTK Client backend without root privileges."""

import os
from pathlib import Path
import shutil
import subprocess
import shlex

BACKEND_URL = "https://github.com/bkerler/mtkclient.git"


def default_backend_dir():
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "fuse" / "backend"


def is_backend_checkout(path):
    path = Path(path).expanduser()
    return (path / "pyproject.toml").is_file() and (path / "uv.lock").is_file()


def install_backend(destination=None, progress=None, log=None):
    """Clone upstream and install its locked environment; return checkout path."""
    destination = Path(destination or default_backend_dir()).expanduser()
    if (destination / "pyproject.toml").is_file() and (destination / "uv.lock").is_file():
        checkout = destination
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Install location is not empty: {destination}")
        if progress:
            progress("Downloading MTK Client…")
        _run(["git", "clone", "--progress", "--depth", "1", BACKEND_URL, str(destination)], log=log)
        checkout = destination

    if not (checkout / "pyproject.toml").is_file():
        raise RuntimeError("Downloaded checkout is missing pyproject.toml")
    if progress:
        progress("Installing backend dependencies…")
    uv = shutil.which("uv")
    if uv:
        _run([uv, "sync", "--project", str(checkout)], cwd=checkout, log=log)
    else:
        venv = checkout / ".venv"
        _run([os.environ.get("PYTHON", "python3"), "-m", "venv", str(venv)], log=log)
        pip = venv / "bin" / "pip"
        _run([str(pip), "install", "-e", str(checkout)], cwd=checkout, log=log)
    return str(checkout.resolve())


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
    rule_file = Path(rule_file or Path(__file__).parent / "assets/52-mtkclient.rules").resolve()
    command = (
        f"{shlex.quote(install)} -m 644 {shlex.quote(str(rule_file))} "
        f"/etc/udev/rules.d/52-mtkclient.rules && "
        f"{shlex.quote(udevadm)} control --reload-rules && "
        f"{shlex.quote(udevadm)} trigger --subsystem-match=usb"
    )
    subprocess.run([pkexec, "/bin/sh", "-c", command], check=True)
