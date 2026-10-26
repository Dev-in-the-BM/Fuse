# MTK Client — Adwaita

An independent GNOME-native client shell built with Python, PyGObject, GTK 4, and libadwaita. The existing Qt GUI and backend under `../mtkclient/` are intentionally untouched.

## Run

Install GTK 4 and libadwaita development/runtime packages plus PyGObject for your distribution, then run:

```sh
python3 Adwaita/mtk_adwaita.py
```

Device detection and initialization use the existing MTK backend in a background thread. The Flash view supports GPT partition read/write/erase, includes device-order or alphabetical sorting, and filters by partition name. Sort and search do not discard partition selections. Storage supports whole-area read/write; Utilities contains key generation, bootloader lock/unlock, and Logs. Backend messages are shown in Logs and printed to the launching terminal. Destructive actions require confirmation. Validate hardware behavior on a supported device before relying on these operations.

## GNOME app-grid shortcut

Make the wrapper executable and install a per-user desktop entry:

```sh
chmod +x Adwaita/run.sh
python3 Adwaita/install-desktop-entry.py
```

The entry appears as **MTK Client** in the GNOME app grid. `run.sh` activates the repository `.venv` when present, then uses `uv run` against `mtkclient/uv.lock` to launch the GTK application. It explicitly reads PyGObject from `/usr/bin/python3`, since GTK bindings are installed by the operating system, not from PyPI. Install `python3-gi`, `gir1.2-gtk-4.0`, and `gir1.2-adw-1` from your distribution packages if needed. For terminal output while developing, launch with `Adwaita/run.sh` rather than invoking `uv run` directly; direct invocation does not configure the distro GI module path.
