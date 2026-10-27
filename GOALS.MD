# Goals

Build a standalone GNOME-native MediaTek client in this repository, using Python, GTK 4, PyGObject, and libadwaita. Use the existing MTK Client checkout as the backend; do not modify its backend or legacy Qt GUI.

The app should provide a clear, non-blocking device connection flow; GPT partition and whole-storage read/write operations; key generation and bootloader utilities; and safe, understandable progress, logs, and errors. Keep destructive actions guarded by explicit confirmation and perform device I/O off the GTK main thread.

Success means the app can select/use a compatible MTK Client checkout, works independently of the Qt GUI, follows GNOME accessibility and interaction conventions, and accurately reports operation outcomes. Hardware behavior must be validated on supported devices before release.
