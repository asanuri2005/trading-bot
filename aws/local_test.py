"""Compatibility entry point for the offline regression suite; no AWS access."""
import pathlib
import sys
import unittest

if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
