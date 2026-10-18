import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from run_script import (
    BusyError, CheckpointError, CheckpointStore, DefinitionError,
    DefinitionMismatchError, RecoveryRequiredError, Script, Status, StepFailedError,
)
from run_script.values import MAX_BYTES


def identity(value):
    return value


def definition(version="1"):
    script = Script("stored", version=version)
    first = script.add_function(identity, {"value": 3}, step_id="first")
    script.add_function(identity, first["value"], step_id="second")
    return script


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def test_resume_in_new_definition_and_completed_noop(self):
        paused = definition().run(checkpoint=self.path, pace="function")
        self.assertEqual(paused.next_step, 1)
        complete = definition().run(checkpoint=self.path)
        self.assertEqual(complete.results, {"first": {"value": 3}, "second": 3})
        self.assertEqual(complete.run_id, paused.run_id)
        self.assertEqual(definition().run(checkpoint=self.path), complete)
        self.assertFalse(CheckpointStore(self.path).lock_path.exists())

    def test_private_atomic_file_and_passive_read(self):
        store = CheckpointStore(self.path)
        self.assertIsNone(store.read())
        self.assertFalse(store.lock_path.exists())
        definition().run(checkpoint=self.path)
        self.assertEqual(store.read()["status"], "completed")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_definition_mismatch_requires_explicit_restart(self):
        first = definition().run(checkpoint=self.path, pace="function")
        with self.assertRaises(DefinitionMismatchError):
            definition("2").run(checkpoint=self.path)
        next_run = definition("2").run(checkpoint=self.path, restart=True)
        self.assertNotEqual(first.run_id, next_run.run_id)

    def test_corrupt_data_never_silently_starts(self):
        for raw in (b'not json private-value', b'{"schema":1,"schema":1}', b'NaN', b'{}',
                    b'"' + b'x' * MAX_BYTES + b'"'):
            self.path.write_bytes(raw)
            with self.assertRaises(CheckpointError) as error:
                definition().run(checkpoint=self.path)
            self.assertNotIn("private-value", str(error.exception))
        self.assertEqual(definition().run(checkpoint=self.path, restart=True).status, Status.COMPLETED)

    def test_invalid_state_shapes_and_prefixes(self):
        definition().run(checkpoint=self.path, pace="function")
        valid = json.loads(self.path.read_text())
        changes = [
            {"schema": 2}, {"extra": 1}, {"cursor": True}, {"cursor": -1},
            {"cursor": 50}, {"cursor": 0}, {"results": {"other": 1}},
            {"active_step": "other"}, {"status": "completed"},
            {"status": "failed"}, {"status": "ready"}, {"error_type": "ValueError"},
            {"status": "running", "active_step": "first"}, {"run_id": "invalid"},
        ]
        for changed in changes:
            with self.subTest(changed=changed):
                self.path.write_text(json.dumps({**valid, **changed}))
                with self.assertRaises(CheckpointError):
                    definition().run(checkpoint=self.path)

    def test_lock_exclusion_and_explicit_stale_lock_removal(self):
        store = CheckpointStore(self.path)
        with store.owned():
            with self.assertRaises(BusyError):
                definition().run(checkpoint=self.path)
            with self.assertRaises(BusyError):
                store.delete()
            with self.assertRaises(BusyError):
                with store.owned():
                    pass
        store.lock_path.write_text("stale")
        with self.assertRaises(BusyError):
            definition().run(checkpoint=self.path)
        self.assertEqual(store.lock_path.read_text(), "stale")
        store.lock_path.unlink()  # the test knows the original owner has stopped
        self.assertEqual(definition().run(checkpoint=self.path).status, Status.COMPLETED)

    @unittest.skipUnless(os.name == "posix", "symlink privileges vary on Windows")
    def test_symlink_and_nonregular_targets_rejected(self):
        target = self.path.with_name("target")
        target.write_text("preserve")
        self.path.symlink_to(target)
        for operation in (lambda: definition().run(checkpoint=self.path),
                          lambda: CheckpointStore(self.path).delete()):
            with self.assertRaises(CheckpointError):
                operation()
        self.assertEqual(target.read_text(), "preserve")
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(CheckpointError):
            definition().run(checkpoint=self.path)

    def test_atomic_failure_preserves_uncertainty_and_cleans_temporary_files(self):
        calls = []
        real_replace = os.replace

        def action():
            calls.append(1)
            return 3

        def replacement(source, target):
            if calls:
                raise OSError("synthetic storage failure")
            return real_replace(source, target)

        def build():
            script = Script("atomic")
            script.add_function(action)
            return script

        with patch("run_script.storage.os.replace", side_effect=replacement):
            with self.assertRaises(StepFailedError):
                build().run(checkpoint=self.path)
        data = CheckpointStore(self.path).read()
        self.assertEqual((data["cursor"], data["active_step"]), (0, "action"))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        with self.assertRaises(RecoveryRequiredError):
            build().run(checkpoint=self.path)
        self.assertEqual(calls, [1])
        self.assertEqual(build().run(checkpoint=self.path, retry=True).status, Status.COMPLETED)
        self.assertEqual(calls, [1, 1])

    def test_failed_step_metadata_and_explicit_retry(self):
        calls = []

        def action():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("private-value")
            return "ok"

        def build():
            script = Script("retry")
            script.add_function(action)
            return script

        with self.assertRaises(StepFailedError):
            build().run(checkpoint=self.path)
        self.assertNotIn("private-value", self.path.read_text())
        self.assertEqual(CheckpointStore(self.path).read()["error_type"], "ValueError")
        with self.assertRaises(RecoveryRequiredError):
            build().run(checkpoint=self.path)
        self.assertEqual(build().run(checkpoint=self.path, retry=True).results, {"action": "ok"})
        self.assertEqual(len(calls), 2)

    def test_interrupt_preserved_and_not_automatically_replayed(self):
        script = Script("interrupt")

        def action():
            raise KeyboardInterrupt

        script.add_function(action)
        with self.assertRaises(KeyboardInterrupt):
            script.run(checkpoint=self.path)
        self.assertEqual(CheckpointStore(self.path).read()["status"], "interrupted")
        with self.assertRaises(RecoveryRequiredError):
            script.run(checkpoint=self.path)

    def test_missing_parent_and_disappeared_progress(self):
        with self.assertRaises(CheckpointError):
            definition().run(checkpoint=self.path / "missing" / "state.json")
        script = definition()
        script.run(checkpoint=self.path, pace="function")
        self.path.unlink()
        with self.assertRaises(CheckpointError):
            script.run(checkpoint=self.path)

    def test_restart_delete_and_storage_switch_are_explicit(self):
        script = definition()
        first = script.run(checkpoint=self.path, pace="function")
        with self.assertRaises(DefinitionError):
            script.run()
        restarted = script.run(checkpoint=self.path, restart=True, pace="function")
        self.assertNotEqual(first.run_id, restarted.run_id)
        self.assertTrue(script.delete_progress())
        self.assertFalse(self.path.exists())
        self.assertEqual(script.run(checkpoint=self.path).status, Status.COMPLETED)
        self.assertTrue(CheckpointStore(self.path).delete())
        self.assertFalse(CheckpointStore(self.path).delete())

    def test_fresh_process_resume_proves_completed_steps_are_skipped(self):
        log = self.path.with_name("calls.txt")
        code = '''
from pathlib import Path
import sys
from run_script import Script
def first():
    with Path(sys.argv[2]).open("a") as stream: stream.write("first\\n")
    return {"value": 7}
def second(value):
    with Path(sys.argv[2]).open("a") as stream: stream.write("second\\n")
    return value
s = Script("process")
ref = s.add_function(first)
s.add_function(second, ref["value"])
print(s.run(checkpoint=sys.argv[1], pace=sys.argv[3]).status)
'''
        for pace, expected in (("function", "paused"), ("auto", "completed"), ("auto", "completed")):
            result = subprocess.run([sys.executable, "-c", code, str(self.path), str(log), pace],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        self.assertEqual(log.read_text().splitlines(), ["first", "second"])

    def test_hard_process_exit_leaves_lock_and_uncertain_marker(self):
        code = '''
import os, sys
from run_script import Script
def action(): os._exit(19)
s = Script("crash")
s.add_function(action)
s.run(checkpoint=sys.argv[1])
'''
        crashed = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                 capture_output=True, timeout=10)
        self.assertEqual(crashed.returncode, 19)
        store = CheckpointStore(self.path)
        self.assertTrue(store.lock_path.exists())
        self.assertEqual(store.read()["active_step"], "action")
        store.lock_path.unlink()  # subprocess has terminated, so ownership is known
        resumed = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                 capture_output=True, timeout=10)
        self.assertNotEqual(resumed.returncode, 19)
        self.assertIn(b"RecoveryRequiredError", resumed.stderr)
