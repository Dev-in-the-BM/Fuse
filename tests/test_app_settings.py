import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_settings


class FirmwareFolderSettingsTests(unittest.TestCase):
    def test_selected_image_folder_persists_and_resolves(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp) / "config"
            settings_file = config_dir / "settings.json"
            image_folder = Path(temp) / "roms" / "release"
            image_folder.mkdir(parents=True)
            with patch.object(app_settings, "APP_CONFIG_DIR", config_dir), \
                    patch.object(app_settings, "SETTINGS_FILE", settings_file):
                app_settings.set_firmware_image_directory(image_folder)
                self.assertEqual(app_settings.get_firmware_image_directory(),
                                 str(image_folder.resolve()))
                self.assertTrue(settings_file.exists())


if __name__ == "__main__":
    unittest.main()
