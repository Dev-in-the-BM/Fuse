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

## Run

Install GTK 4 and libadwaita development/runtime packages plus PyGObject for your distribution, then run:

```sh
./run.sh
```

### Install from source with one command

On a system that already has Python 3, PyGObject, GTK 4, Cairo, and libadwaita
1.4 or newer, install Fuse into your user account and add it to the GNOME app
grid directly from the current `main` source with:

```sh
curl -fsSL https://raw.githubusercontent.com/Dev-in-the-BM/Fuse/main/install.sh | sh
```

This does not install a `.deb`, Flatpak, or AppImage, and does not use `sudo`
or a system package manager. It installs source under `~/.local/opt/fuse/` and
creates a per-user desktop entry and icons. The MTK Client backend can then be
installed from Fuse's Preferences. The GTK/Python runtime is an OS prerequisite;
the script checks for it and exits with guidance instead of installing system
packages.

Install MTK Client from **Preferences → Backend installation**, or choose an existing checkout under **MTK Client backend**. Installation downloads it into the user's data directory and uses `uv` when available, otherwise a private venv and pip. It does not use sudo. USB access can be configured separately from **Preferences → USB device access**. Where PolicyKit is available, the app requests authorization for a focused udev rule; otherwise it displays manual commands. The rule grants the active local user access to MediaTek and common Android USB vendor IDs. Reconnect the device after setup. The selection is saved per user and used by both `./run.sh` and the GNOME app-grid launcher. For temporary overrides during development, set `MTKCLIENT_DIR` to a directory containing the checkout's `pyproject.toml` and `uv.lock`:

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
