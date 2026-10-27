# Adwaita Client Goals

## Goal
Build a separate GNOME-native MediaTek client in `Adwaita/` using Python, PyGObject, GTK 4, and libadwaita. The existing `mtkclient/` implementation—including its Qt GUI—is the authoritative backend and must not be modified.

## Frontend scope
- Establish an Adwaita application/window with native navigation and useful device-connection feedback.
- Make the application runnable independently of the legacy Qt GUI.
- Support GPT partition read/write/erase and whole-area read/write through the existing backend.
- Include key generation, bootloader lock/unlock, and loader/preloader/IoT/serial connection options.
- Gate destructive actions with explicit confirmations and run I/O away from the GTK main loop.
- Document run requirements, architecture, and current limitations.

## Design decisions
- `Adw.ApplicationWindow` with `Adw.HeaderBar`, `Adw.ViewStack`, and `Adw.ViewSwitcher` for the small set of primary views.
- Native libadwaita controls, keyboard-accessible actions, clear empty/loading/error states, and system color scheme.
- No custom branding or copied Qt widgets required for the initial shell.

## Acceptance criteria
- Launches with GTK 4/libadwaita and does not import or alter the Qt GUI.
- Connection view provides actionable setup information and non-blocking status.
- Read/write/erase actions delegate to the existing backend and report progress/errors.
- Source remains isolated under `Adwaita/`; root goals, progress, and contributor instructions are maintained.
