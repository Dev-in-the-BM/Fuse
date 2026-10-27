# Progress

- **Implemented:** GTK 4/libadwaita app shell and connection guide; persistent backend-checkout selection; device connection options; GPT partition discovery, filtering, sorting, image assignment, read/write/erase; whole-storage read/write; key generation and bootloader lock/unlock; logs; confirmations; background I/O; and backend-reported progress/ETA where available.
- **Launcher:** `run.sh` uses the saved checkout (or `MTKCLIENT_DIR` override), prepares system PyGObject access without shadowing the backend environment, and can open Preferences when no checkout is configured. A GNOME app-grid installer is included.
- **Validation:** Python and shell syntax checks, GTK/libadwaita imports, settings persistence, and backend dependency imports pass. No successful end-to-end test on connected hardware has been recorded.
- **Next:** Validate connection and operations on supported devices; address any hardware-specific failures before release.
