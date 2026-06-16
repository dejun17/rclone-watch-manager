import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "rclone_watch_manager.py"

spec = importlib.util.spec_from_file_location("rwm", MODULE_PATH)
rwm = importlib.util.module_from_spec(spec)
sys.modules["rwm"] = rwm
spec.loader.exec_module(rwm)


class BasicTests(unittest.TestCase):
    def test_sanitize_name(self):
        self.assertEqual(rwm.sanitize_name("My Photos!"), "my-photos")
        self.assertEqual(rwm.sanitize_name("  Docs_Backup.01  "), "docs_backup.01")

    def test_app_metadata(self):
        self.assertEqual(rwm.APP_NAME, "rclone-watch-manager")
        self.assertEqual(rwm.APP_DISPLAY_NAME, "Rclone Watch Manager")
        self.assertEqual(rwm.APP_VERSION, "1.0.1")

    def test_safe_extract_tar_exists(self):
        self.assertTrue(hasattr(rwm, "safe_extract_tar"))


if __name__ == "__main__":
    unittest.main()
