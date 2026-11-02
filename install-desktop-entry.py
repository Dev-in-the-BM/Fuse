#!/usr/bin/env python3
"""Install the current checkout's launcher into the GNOME app grid."""

from pathlib import Path
import shutil


app_dir = Path(__file__).resolve().parent
desktop_dir = Path.home() / ".local/share/applications"
desktop_dir.mkdir(parents=True, exist_ok=True)
desktop_file = desktop_dir / "io.github.mtkclient.Adwaita.desktop"
launcher = str(app_dir / "run.sh")
app_id = "io.github.mtkclient.Adwaita"

# Install standard hicolor sizes so the shell, app grid, and other launchers
# can select the best raster for their display scale. Keep the SVG as the
# scalable icon for environments that support it.
icon_sizes = (16, 24, 32, 48, 64, 96, 128, 256, 512)
icon_theme_dir = Path.home() / ".local/share/icons/hicolor"
for size in icon_sizes:
    icon_dir = icon_theme_dir / f"{size}x{size}/apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        app_dir / "assets" / f"fuse-mtk-app-icon-{size}.png",
        icon_dir / f"{app_id}.png",
    )

scalable_icon_dir = icon_theme_dir / "scalable/apps"
scalable_icon_dir.mkdir(parents=True, exist_ok=True)
shutil.copy2(
    app_dir / "assets" / "fuse-mtk-app-icon.svg",
    scalable_icon_dir / f"{app_id}.svg",
)

# Desktop Entry Exec quoting uses double quotes and backslash escaping.
quoted_launcher = '"' + launcher.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
desktop_file.write_text(
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=Fuse\n"
    "GenericName=MTK Client\n"
    "Comment=Fuse, a GNOME-native MTK Client for MediaTek devices\n"
    "Keywords=MTK Client;MTK;MediaTek;Fuse;\n"
    f"Exec={quoted_launcher}\n"
    f"Icon={app_id}\n"
    "Terminal=false\n"
    "Categories=Utility;\n"
    "StartupNotify=true\n"
    "StartupWMClass=io.github.mtkclient.Adwaita\n"
    "",
    encoding="utf-8",
)
desktop_file.chmod(0o644)
print(f"Installed app-grid launcher: {desktop_file}")
print(f"Installed {len(icon_sizes)} PNG sizes and scalable SVG icon for {app_id}")
