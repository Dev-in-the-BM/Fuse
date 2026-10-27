# Adwaita Client Progress

## 2026-10-26
- [x] Recorded scope and constraints: separate GNOME client; do not modify `mtkclient/`.
- [x] Reviewed existing Qt GUI and GNOME HIG guidance.
- [x] Implement initial GTK 4/libadwaita application shell with Device, Operations, and Logs views.
- [x] Verify Python syntax and document runtime dependencies.
- [x] Add `run.sh` wrapper and per-user GNOME app-grid desktop-entry installer.
- [x] Connect the Adwaita client to the existing MTK bootrom/preloader initialization flow in a worker thread.
- [x] Mirror verbose backend logs to both the Logs view and terminal.
- [x] Implement GPT partition discovery and partition read/write/erase workflows.
- [x] Implement whole-area read/write, key generation, bootloader lock/unlock, and connection options.
- [x] Add destructive-action confirmations and run storage operations off the UI thread.
- [x] Verify GTK widget construction and read/write/erase dispatch with a fake backend.
- [x] Replace the operation waiting pulse with backend-reported byte progress and a remaining-time estimate when transfer size is known; retain an estimating pulse for operations without a known size.

## Current status
The legacy Qt GUI is `mtkclient/mtk_gui.py`; backend code is under `mtkclient/mtkclient/`, which remains untouched. The GNOME client now exposes GPT partition reads, writes, erases, whole-area read/write, key generation, bootloader lock/unlock, and connection options. Python syntax, GTK widget construction, and fake-backend dispatch checks pass; no connected-device test has been performed. Install the app-grid shortcut with `python3 Adwaita/install-desktop-entry.py`.
