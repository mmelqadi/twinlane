"""Private unittest subprocess driver; diagnostics never carry result records."""

import json
import os
import sys
import time
import unittest


def leaves(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from leaves(test)
        else:
            yield test


def main(target, result_path):
    # Match `python -m unittest`: the driver's own directory must not shadow a
    # module selected from a different --tests-dir with the same basename.
    sys.path[0] = os.getcwd()
    # Open before imports: even an import that exits leaves an empty result file.
    with open(result_path, "w", encoding="utf-8", buffering=1) as records:
        def emit(record):
            records.write(json.dumps(record) + "\n")
            records.flush()

        suite = unittest.defaultTestLoader.loadTestsFromName(target)
        tests = list(leaves(suite))
        emit({"event": "loaded", "tests": [test.id() for test in tests]})
        completed = set()

        class Result(unittest.TextTestResult):
            active = None
            status = None

            def startTest(self, test):
                super().startTest(test)
                self.active = test
                self.status = None

            def addSuccess(self, test):
                super().addSuccess(test)
                self.status = "ok"

            def addFailure(self, test, err):
                super().addFailure(test, err)
                if self.status != "error":
                    self.status = "fail"

            def addError(self, test, err):
                super().addError(test, err)
                self.status = "error"

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                if getattr(self, "status", None) not in ("fail", "error"):
                    self.status = "skipped"

            def addExpectedFailure(self, test, err):
                super().addExpectedFailure(test, err)
                self.status = "expected_failure"

            def addUnexpectedSuccess(self, test):
                super().addUnexpectedSuccess(test)
                self.status = "unexpected_success"

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is not None:
                    if not issubclass(err[0], test.failureException):
                        self.status = "error"
                    elif self.status != "error":
                        self.status = "fail"

            def stopTest(self, test):
                super().stopTest(test)
                if self.active is test and self.status is not None:
                    emit({"event": "outcome", "test": test.id(), "status": self.status})
                    completed.add(test.id())
                self.active = None
                self.status = None

        start = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
        # Module/class setup failures are not attached to runnable test methods.
        for test, _ in result.errors:
            if isinstance(test, unittest.suite._ErrorHolder):
                emit({"event": "fixture_error", "test": test.id(), "status": "error"})
        # unittest represents a skipped module/class fixture as one _ErrorHolder.
        # Record an explicit skip for each affected loader entry.
        for test, _ in result.skipped:
            if isinstance(test, unittest.suite._ErrorHolder):
                scope = test.id().partition(" (")[2].removesuffix(")")
                for child in tests:
                    if child.id().startswith(scope + ".") and child.id() not in completed:
                        emit({"event": "outcome", "test": child.id(), "status": "skipped"})
                        completed.add(child.id())
        emit({"event": "finished", "tests_ran": result.testsRun,
              "seconds": time.monotonic() - start, "success": result.wasSuccessful()})
        return int(not result.wasSuccessful())


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
