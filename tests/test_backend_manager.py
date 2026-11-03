import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

import backend_manager


class BackendInstallTests(unittest.TestCase):
    def test_default_checkout_uses_fuse_data_directory(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/fuse-test-data"}):
            self.assertEqual(
                backend_manager.default_backend_dir(),
                Path("/tmp/fuse-test-data/fuse/backend"),
            )

    def test_backend_python_paths_include_source_and_venv_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            site_packages = checkout / ".venv/lib/python3.13/site-packages"
            site_packages.mkdir(parents=True)
            self.assertEqual(
                backend_manager.backend_python_paths(checkout),
                [checkout.resolve(), site_packages.resolve()],
            )

    def test_filter_removes_only_qt_dependencies(self):
        requirements = """\
wheel
PySide6==6.10.1
shiboken6==6.10.1
PySide6_Essentials==6.10.1
pyusb
pyserial
keystone
"""
        self.assertEqual(
            backend_manager.filter_cli_requirements(requirements),
            "wheel\npyusb\npyserial\nkeystone\n",
        )

    def test_existing_checkout_installs_upstream_requirements_without_qt(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            (checkout / "pyproject.toml").touch()
            (checkout / "uv.lock").touch()
            (checkout / "requirements.txt").write_text(
                "wheel\nPySide6\nshiboken6\npyusb\n", encoding="utf-8"
            )
            commands = []

            def capture_command(command, **_kwargs):
                commands.append(command)
                if "--requirement" in command:
                    requirements_file = Path(command[command.index("--requirement") + 1])
                    self.assertEqual(
                        requirements_file.read_text(encoding="utf-8"),
                        "wheel\npyusb\n",
                    )

            with patch.dict(os.environ, {"PYTHON": ""}), \
                    patch.object(backend_manager.sys, "_base_executable", "/usr/bin/python3"), \
                    patch.object(backend_manager.shutil, "which", return_value=None), \
                    patch.object(backend_manager, "_run", side_effect=capture_command):
                backend_manager.install_backend(checkout)
            self.assertEqual(len(commands), 2)
            self.assertIn("venv", commands[0])
            self.assertIn("--clear", commands[0])
            self.assertEqual(commands[0][0], "/usr/bin/python3")
            install_command = commands[1]
            self.assertEqual(install_command[1:4], ["-m", "pip", "install"])
            self.assertIn("--requirement", install_command)

    def test_uses_uv_cache_without_syncing_project_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            (checkout / "pyproject.toml").touch()
            (checkout / "uv.lock").touch()
            (checkout / "requirements.txt").write_text(
                "PySide6\nshiboken6\npyusb\n", encoding="utf-8"
            )
            commands = []

            def capture_command(command, **_kwargs):
                commands.append(command)
                if command[:3] == ["/usr/bin/uv", "pip", "install"]:
                    requirements_file = Path(command[command.index("--requirements") + 1])
                    self.assertEqual(
                        requirements_file.read_text(encoding="utf-8"),
                        "pyusb\n",
                    )

            with patch.object(backend_manager.shutil, "which", return_value="/usr/bin/uv"), \
                    patch.object(backend_manager, "_run", side_effect=capture_command):
                backend_manager.install_backend(checkout)

            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][:3], ["/usr/bin/uv", "venv", "--clear"])
            self.assertEqual(commands[1][:3], ["/usr/bin/uv", "pip", "install"])
            self.assertNotIn("sync", commands[0] + commands[1])

    def test_archive_extraction_writes_checkout_files(self):
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "backend.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                content = b"[project]\nname = 'mtkclient'\n"
                info = tarfile.TarInfo("mtkclient-main/pyproject.toml")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            destination = Path(temp) / "checkout"
            destination.mkdir()
            backend_manager._extract_backend_archive(archive_path, destination)
            self.assertEqual(
                (destination / "pyproject.toml").read_bytes(), content
            )

    def test_archive_extraction_rejects_path_traversal(self):
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "backend.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                content = b"unsafe"
                info = tarfile.TarInfo("mtkclient-main/../escape")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            destination = Path(temp) / "checkout"
            destination.mkdir()
            with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                backend_manager._extract_backend_archive(archive_path, destination)


if __name__ == "__main__":
    unittest.main()
