# <img src="assets/fuse-mtk-app-icon.svg" width="64" alt=""> Fuse

A modern, GNOME-native MTK client frontend, built with Python, PyGObject, GTK 4, and libadwaita.

## Features

- Read, write, and delete GPT partitions. Search, sort, and pin partitions.
- Find firmware image sources: Not sure which IMG is on each partition?
  Choose a folder containing possible images. Fuse compares their fingerprints with device partitions and shows which image likely produced each partition. Full verification is also available.
- Generate device keys and lock or unlock the bootloader.
- Use an existing MTK Client installation as its backend to save storage, or download and install the backend with one click.

## Screenshots

**Connecting to a device**

![Device connection screen](screenshots/connecting.png)

**Managing partitions**

![GPT partition management screen](screenshots/flashing-screen.png)

**Finding firmware image sources**

![Firmware image source identification screen](screenshots/source-indentifier.png)

## Install

Choose one of these options:

### One-command install

```sh
curl -fsSL https://raw.githubusercontent.com/Dev-in-the-BM/Fuse/main/install.sh | sh
```

This installs Fuse from source. If required system dependencies are missing, the installer shows the packages and asks before installing them with your package manager. It supports Debian/Ubuntu, Fedora/RHEL, openSUSE, Arch, and Alpine.
Python 3.10+ and libadwaita 1.4+ are required.

### Download a release

Get the latest `.deb`, AppImage, or Flatpak from
[GitHub Releases](https://github.com/Dev-in-the-BM/Fuse/releases):


The `.deb` uses your system GTK/Python runtime. The AppImage and Flatpak bundle
the application runtime and MTK Client backend. With the one-command source
install or `.deb`, install the backend separately from Fuse's Preferences or
select an existing checkout.

If installing from a source checkout for development, see [Run from source](#run-from-source).

<details>
  <summary><h2>If you like playing with the source</h2></summary>
## Run from source

For development, install GTK 4, libadwaita 1.4+, Cairo, and PyGObject using your
distribution's packages, then run:

```sh
./run.sh
```

Install MTK Client from **Preferences → Backend installation**, or choose an existing checkout under **MTK Client backend**. Installation downloads it into the user's data directory and uses `uv` when available, otherwise a private venv and pip. USB access can be configured separately from **Preferences → USB device access**. For temporary overrides during development, set `MTKCLIENT_DIR` to a directory containing the checkout's `pyproject.toml` and `uv.lock`:

```sh
MTKCLIENT_DIR=/path/to/mtk-client ./run.sh
```

## GNOME app-grid shortcut

Make the wrapper executable and install a per-user desktop entry:

```sh
chmod +x run.sh
python3 install-desktop-entry.py
```

The entry appears as **Fuse** in the GNOME app grid. The installer registers Fuse's app icon at multiple hicolor sizes and as a scalable SVG, for the launcher and dock; the same mark is shown in **About Fuse**. `run.sh` activates this project's `.venv` when present, then uses `uv run` against the selected backend checkout. It explicitly reads PyGObject from `/usr/bin/python3`, since GTK bindings are installed by the operating system, not from PyPI. Install `python3-gi`, `gir1.2-gtk-4.0`, and `gir1.2-adw-1` from your distribution packages if needed. For terminal output while developing, launch with `./run.sh` rather than invoking `uv run` directly; direct invocation does not configure the distro GI module path.

## Debian package

Build a native `.deb` from the repository root on Debian or Ubuntu after installing `build-essential` and `debhelper`:

```sh
dpkg-buildpackage -us -uc -b
sudo apt install ../quest.devinthebm.fuse_*.deb
```

The package installs Fuse, its desktop entry and icons, and the USB udev rule. It depends on the system GTK/Python runtime and the tools required for the optional per-user backend installer; it does not bundle MTK Client. Backend installation still happens from Preferences and requires an internet connection. Reconnect the device after installing the package so udev can apply the rule.

  ```
</details>
