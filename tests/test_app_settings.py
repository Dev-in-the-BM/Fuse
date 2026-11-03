import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_settings


class FirmwareFolderSettingsTests(unittest.TestCase):
    def test_auto_identify_setting_defaults_and_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp) / "config"
            settings_file = config_dir / "settings.json"
            with patch.object(app_settings, "APP_CONFIG_DIR", config_dir), \
                    patch.object(app_settings, "SETTINGS_FILE", settings_file):
                self.assertFalse(app_settings.get_auto_identify_partitions())
                app_settings.set_auto_identify_partitions(True)
                self.assertTrue(app_settings.get_auto_identify_partitions())

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

    def test_backend_log_limit_defaults_and_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp) / "config"
            settings_file = config_dir / "settings.json"
            with patch.object(app_settings, "APP_CONFIG_DIR", config_dir), \
                    patch.object(app_settings, "SETTINGS_FILE", settings_file):
                self.assertEqual(app_settings.get_backend_log_limit_mb(), 50)
                app_settings.set_backend_log_limit_mb(250)
                self.assertEqual(app_settings.get_backend_log_limit_mb(), 250)

    def test_backend_log_limit_is_clamped_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp) / "config"
            settings_file = config_dir / "settings.json"
            with patch.object(app_settings, "APP_CONFIG_DIR", config_dir), \
                    patch.object(app_settings, "SETTINGS_FILE", settings_file):
                app_settings._save_settings({"backend_log_limit_mb": 1})
                self.assertEqual(app_settings.get_backend_log_limit_mb(), 50)
                with self.assertRaises(ValueError):
                    app_settings.set_backend_log_limit_mb(49)


if __name__ == "__main__":
    unittest.main()
