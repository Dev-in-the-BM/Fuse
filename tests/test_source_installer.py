import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_installer_functions():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    payload = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    module = ast.parse(payload, filename="install.sh")
    # Keep definitions and constants, but don't execute the install entry point.
    assert isinstance(module.body[-1], ast.Try)
    module.body.pop()
    namespace = {"__name__": "fuse_installer_test"}
    exec(compile(module, "install.sh", "exec"), namespace)
    return namespace


class SourceInstallerDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer_functions()

    def test_supported_package_families_have_only_system_runtime_dependencies(self):
        dependencies = self.installer["DEPENDENCIES"]
        self.assertEqual(set(dependencies), {"apt", "dnf", "zypper", "pacman", "apk"})
        flattened = {package for packages in dependencies.values() for package in packages}
        self.assertFalse(any(name.lower().startswith(("pyside", "shiboken"))
                             for name in flattened))
        self.assertFalse(any("mtkclient" in name.lower() for name in flattened))

    def test_installer_requests_explicit_consent(self):
        tty = Mock()
        tty.__enter__ = Mock(return_value=tty)
        tty.__exit__ = Mock(return_value=False)
        tty.readline.return_value = "yes\n"
        with patch("builtins.open", return_value=tty), \
             patch.object(os, "geteuid", return_value=1000), \
             patch.object(shutil, "which", return_value="/usr/bin/sudo"):
            self.assertTrue(self.installer["consent_to_dependencies"]("apt", ["GTK runtime"]))

        tty.readline.return_value = "no\n"
        with patch("builtins.open", return_value=tty), \
             patch.object(os, "geteuid", return_value=1000), \
             patch.object(shutil, "which", return_value="/usr/bin/sudo"):
            self.assertFalse(self.installer["consent_to_dependencies"]("apt", ["GTK runtime"]))

    def test_package_manager_commands_are_family_specific_and_mockable(self):
        expected = {
            "apt": [["/usr/bin/sudo", "apt-get", "update"], ["/usr/bin/sudo", "apt-get", "install", "-y"]],
            "dnf": [["/usr/bin/sudo", "dnf", "install", "-y"]],
            "zypper": [["/usr/bin/sudo", "zypper", "--non-interactive", "install"]],
            "pacman": [["/usr/bin/sudo", "pacman", "-S", "--needed", "--noconfirm"]],
            "apk": [["/usr/bin/sudo", "apk", "add", "--no-cache"]],
        }
        for manager, prefixes in expected.items():
            with self.subTest(manager=manager), \
                 patch.object(shutil, "which", return_value="/usr/bin/sudo"), \
                 patch.object(subprocess, "run") as run:
                self.installer["install_dependencies"](manager)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual([command[:len(prefix)] for command, prefix in zip(commands, prefixes)], prefixes)
                self.assertTrue(all(call.kwargs.get("check") is True for call in run.call_args_list))

    def test_package_manager_detection_uses_os_release_family(self):
        cases = (
            ('ID="ubuntu"\nID_LIKE="debian"\n', "apt"),
            ('ID="fedora"\n', "dnf"),
            ('ID="opensuse-tumbleweed"\nID_LIKE="suse"\n', "zypper"),
            ('ID="manjaro"\nID_LIKE="arch"\n', "pacman"),
            ('ID="alpine"\n', "apk"),
        )
        executable = {"apt": "apt-get", "dnf": "dnf", "zypper": "zypper",
                      "pacman": "pacman", "apk": "apk"}
        for os_release, expected in cases:
            with self.subTest(manager=expected), \
                 patch.object(Path, "read_text", return_value=os_release), \
                 patch.object(shutil, "which", side_effect=lambda name: "/bin/" + name if name == executable[expected] else None):
                self.assertEqual(self.installer["detect_package_manager"](), expected)


if __name__ == "__main__":
    unittest.main()
