"""Run offline baseline checks; --strict makes known bugs ordinary failures."""

import argparse
import unittest
from tests.support import ROOT


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Expose known bugs as failing requirements (nonzero exit expected until repaired)")
    options = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    if options.strict:
        for case in cases(suite):
            method = getattr(type(case), case._testMethodName)
            if getattr(method, "__unittest_expecting_failure__", False):
                method.__unittest_expecting_failure__ = False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
