import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
INSTALLER = APP_DIR / "install-desktop-entry.py"
APP_ID = "io.github.mtkclient.Adwaita"
ICON_SIZES = (16, 24, 32, 48, 64, 96, 128, 256, 512)


class DesktopEntryInstallTests(unittest.TestCase):
    def test_installs_app_icon_and_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env["HOME"] = temp
            subprocess.run(
                [sys.executable, str(INSTALLER)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            data_dir = Path(temp) / ".local/share"
            desktop_file = data_dir / f"applications/{APP_ID}.desktop"
            desktop_entry = desktop_file.read_text(encoding="utf-8")
            self.assertIn(f"Icon={APP_ID}\n", desktop_entry)
            self.assertNotIn("Icon=phone-symbolic", desktop_entry)

            for size in ICON_SIZES:
                installed_icon = (
                    data_dir / f"icons/hicolor/{size}x{size}/apps/{APP_ID}.png"
                )
                self.assertTrue(installed_icon.is_file(), installed_icon)
                self.assertEqual(
                    installed_icon.read_bytes(),
                    (APP_DIR / f"assets/fuse-mtk-app-icon-{size}.png").read_bytes(),
                )

            scalable_icon = data_dir / f"icons/hicolor/scalable/apps/{APP_ID}.svg"
            self.assertEqual(
                scalable_icon.read_bytes(),
                (APP_DIR / "assets/fuse-mtk-app-icon.svg").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
