import shutil
import tempfile
import unittest
from pathlib import Path

from support import make_config  # noqa: F401  (adds the repository root to sys.path)

from bot.__main__ import acquire_instance_lock


class InstanceLockTests(unittest.TestCase):
    def test_only_one_bot_instance_holds_the_lock(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        first = acquire_instance_lock(tmp)
        self.assertIsNotNone(first)
        self.assertIsNone(acquire_instance_lock(tmp))
        first.close()
        again = acquire_instance_lock(tmp)
        self.assertIsNotNone(again)
        again.close()


if __name__ == "__main__":
    unittest.main()
