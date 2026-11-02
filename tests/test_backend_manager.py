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

    def test_existing_checkout_installs_with_uv(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            (checkout / "pyproject.toml").touch()
            (checkout / "uv.lock").touch()
            with patch.object(backend_manager.shutil, "which", return_value="/bin/uv"), \
                    patch.object(backend_manager, "_run") as run:
                result = backend_manager.install_backend(checkout)
            self.assertEqual(result, str(checkout.resolve()))
            self.assertEqual(run.call_args.args[0][:2], ["/bin/uv", "sync"])

    def test_existing_checkout_uses_venv_and_pip_without_uv(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            (checkout / "pyproject.toml").touch()
            (checkout / "uv.lock").touch()
            with patch.object(backend_manager.shutil, "which", return_value=None), \
                    patch.object(backend_manager, "_run") as run:
                backend_manager.install_backend(checkout)
            self.assertEqual(run.call_count, 2)
            self.assertIn("venv", run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[1].args[0][1], "install")


if __name__ == "__main__":
    unittest.main()
