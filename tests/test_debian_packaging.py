import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "quest.devinthebm.fuse"
PACKAGE = ROOT / "debian"


class DebianPackagingTests(unittest.TestCase):
    def test_app_id_is_consistent(self):
        app = (ROOT / "mtk_adwaita.py").read_text(encoding="utf-8")
        desktop = (ROOT / "debian" / f"{APP_ID}.desktop").read_text(encoding="utf-8")
        self.assertIn(f'APP_ID = "{APP_ID}"', app)
        self.assertIn(f"Icon={APP_ID}\n", desktop)
        self.assertIn(f"StartupWMClass={APP_ID}\n", desktop)
        self.assertIn("Exec=fuse\n", desktop)

    def test_package_installs_runtime_and_usb_rule(self):
        install = (PACKAGE / f"{APP_ID}.install").read_text(encoding="utf-8")
        control = (ROOT / "debian" / "control").read_text(encoding="utf-8")
        for module in (
            "app_settings.py",
            "backend_manager.py",
            "mtk_adwaita.py",
            "mtk_image_identify.py",
            "sitecustomize.py",
        ):
            self.assertIn(f"{module} usr/lib/fuse", install)
            self.assertTrue((ROOT / module).is_file())
        self.assertIn("assets/52-mtkclient.rules lib/udev/rules.d", install)
        self.assertIn("python3-gi", control)
        self.assertIn("gir1.2-gtk-4.0", control)
        self.assertIn("gir1.2-adw-1", control)

    def test_launcher_targets_installed_app(self):
        launcher = (ROOT / "debian" / "fuse").read_text(encoding="utf-8")
        self.assertIn("APP_DIR=/usr/lib/fuse", launcher)
        self.assertIn("/usr/bin/python3 \"$APP_DIR/mtk_adwaita.py\"", launcher)


if __name__ == "__main__":
    unittest.main()
