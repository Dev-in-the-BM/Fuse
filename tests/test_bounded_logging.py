import logging
import tempfile
import unittest
from pathlib import Path

import bounded_logging


class BoundedBackendLoggingTests(unittest.TestCase):
    def tearDown(self):
        bounded_logging.restore_file_handler_for_tests()

    def test_backend_handlers_share_one_bounded_rotating_file(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp)
            log_dir = backend / "logs"
            log_dir.mkdir()
            handler = bounded_logging.configure_backend_file_logging(
                backend, 1024
            )
            first = logging.getLogger("bounded-test.first")
            second = logging.getLogger("bounded-test.second")
            first.setLevel(logging.DEBUG)
            second.setLevel(logging.DEBUG)
            first.propagate = False
            second.propagate = False

            first_handler = logging.FileHandler(log_dir / "log.txt")
            second_handler = logging.FileHandler(backend / "log.txt")
            self.assertIs(first_handler, second_handler)
            first.addHandler(first_handler)
            second.addHandler(second_handler)
            try:
                for index in range(40):
                    first.debug("first-%02d %s", index, "a" * 90)
                    second.debug("second-%02d %s", index, "b" * 90)
                    self.assertLessEqual(self._total_log_bytes(log_dir), 1024)
                first.debug("oversized %s", "x" * 4000)
                self.assertLessEqual(self._total_log_bytes(log_dir), 1024)
            finally:
                first.removeHandler(first_handler)
                second.removeHandler(second_handler)

            self.assertLessEqual(self._total_log_bytes(log_dir), 1024)
            self.assertTrue((log_dir / "log.txt").exists())
            self.assertTrue(list(log_dir.glob("log.txt.*")))

            parent = logging.getLogger("bounded-test.hierarchy")
            child = logging.getLogger("bounded-test.hierarchy.child")
            parent.setLevel(logging.DEBUG)
            child.setLevel(logging.DEBUG)
            parent.propagate = False
            parent.addHandler(handler)
            child.addHandler(handler)
            try:
                child.debug("deduplicated-record")
            finally:
                child.removeHandler(handler)
                parent.removeHandler(handler)
            handler.flush()
            self.assertLessEqual(self._total_log_bytes(log_dir), 1024)
            combined = b"".join(path.read_bytes() for path in log_dir.glob("log.txt*"))
            self.assertEqual(combined.count(b"deduplicated-record"), 1)
            self.assertIn(b"Oversized log record truncated", combined)

    def test_oversized_existing_log_is_trimmed_to_budget_on_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp)
            log_dir = backend / "logs"
            log_dir.mkdir()
            log_path = log_dir / "log.txt"
            log_path.write_bytes(b"old-data\n" * 200 + b"LATEST\n")

            bounded_logging.configure_backend_file_logging(backend, 1024)

            self.assertLessEqual(self._total_log_bytes(log_dir), 1024)
            content = b"".join(
                path.read_bytes()
                for path in sorted(log_dir.glob("log.txt*"), reverse=True)
            )
            self.assertIn(b"LATEST\n", content)
            self.assertIn(b"Older log data trimmed", content)

    @staticmethod
    def _total_log_bytes(log_dir):
        return sum(path.stat().st_size for path in log_dir.glob("log.txt*"))


if __name__ == "__main__":
    unittest.main()
