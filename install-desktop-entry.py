#!/usr/bin/env python3
"""Install the current checkout's launcher into the GNOME app grid."""

from pathlib import Path
import shutil


app_dir = Path(__file__).resolve().parent
desktop_dir = Path.home() / ".local/share/applications"
desktop_dir.mkdir(parents=True, exist_ok=True)
desktop_file = desktop_dir / "io.github.mtkclient.Adwaita.desktop"
launcher = str(app_dir / "run.sh")

# Desktop Entry Exec quoting uses double quotes and backslash escaping.
quoted_launcher = '"' + launcher.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
desktop_file.write_text(
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=MTK Client\n"
    "GenericName=MediaTek Device Client\n"
    "Comment=GNOME-native client for MediaTek devices\n"
    f"Exec={quoted_launcher}\n"
    "Icon=phone-symbolic\n"
    "Terminal=false\n"
    "Categories=Utility;\n"
    "StartupNotify=true\n"
    "StartupWMClass=io.github.mtkclient.Adwaita\n"
    "",
    encoding="utf-8",
)
desktop_file.chmod(0o644)
print(f"Installed app-grid launcher: {desktop_file}")
