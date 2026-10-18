from contextlib import redirect_stdout
from io import StringIO
from threading import Event as ThreadEvent, Thread
import unittest

from func_seq import (
    BusyError, Control, DefinitionError, RecoveryRequiredError, ResolutionError,
    Script, Status, StepFailedError, pause,
)


class RuntimeTests(unittest.TestCase):
    def test_order_references_and_completed_resume(self):
        calls = []
        script = Script("order")
        first = script.add_function(lambda: {"items": [1]}, step_id="first")

        def mutate(data):
            calls.append(data["items"].pop())
            return "done"

        script.add_function(mutate, first, step_id="second")
        result = script.run()
        result.results["first"]["items"].clear()
        self.assertEqual(script.run().results["first"], {"items": [1]})
        self.assertEqual(calls, [1])
        self.assertEqual(result.status, Status.COMPLETED)
        with self.assertRaises(DefinitionError):
            script.add_pause(step_id="late")
        script.run(restart=True)
        self.assertEqual(calls, [1, 1])

    def test_function_pace_and_empty_script(self):
        script = Script("steps")
        script.add_function(lambda: 1, step_id="one")
        script.add_function(lambda: 2, step_id="two")
        self.assertEqual(script.run(pace="function").next_step, 1)
        self.assertEqual(script.run(pace="function").status, Status.COMPLETED)
        self.assertEqual(Script("empty").run().status, Status.COMPLETED)

    def test_explicit_pause_is_cooperative_and_pace_specific(self):
        calls = []

        def first():
            calls.append("before")
            pause()
            calls.append("after")

        for pace, expected in (("auto", 2), ("pause", 1)):
            script = Script("explicit")
            script.add_function(first)
            script.add_function(lambda: calls.append("last"), step_id="last")
            self.assertEqual(script.run(pace=pace).next_step, expected)
        self.assertEqual(calls, ["before", "after", "last", "before", "after"])
        pause()  # no active script

    def test_pause_marker(self):
        script = Script("markers")
        script.add_pause(step_id="break")
        script.add_function(lambda: 1, step_id="after")
        self.assertEqual(script.run(pace="pause").status, Status.PAUSED)
        self.assertEqual(script.run(pace="pause").results, {"break": None, "after": 1})

    def test_external_control_before_and_during_call(self):
        control = Control()
        control.request_pause()
        calls = []
        script = Script("stop")

        def first():
            calls.append("first")
            control.request_pause()

        script.add_function(first)
        script.add_function(lambda: calls.append("second"), step_id="second")
        self.assertEqual(script.run(control=control).next_step, 0)
        control.reset()
        self.assertEqual(script.run(control=control).next_step, 1)
        self.assertEqual(calls, ["first"])
        control.reset()
        self.assertEqual(script.run(control=control).status, Status.COMPLETED)

    def test_failures_require_explicit_recovery(self):
        calls = []

        def failing():
            calls.append(1)
            raise ValueError("private data")

        script = Script("failed")
        script.add_function(failing)
        for options, count in (({}, 1), ({}, 1), ({"retry": True}, 2)):
            with self.assertRaises((StepFailedError, RecoveryRequiredError)) as caught:
                script.run(**options)
            self.assertNotIn("private data", str(caught.exception))
            self.assertEqual(len(calls), count)

    def test_invalid_result_is_uncertain(self):
        script = Script("invalid")
        script.add_function(lambda: object(), step_id="bad")
        with self.assertRaises(StepFailedError):
            script.run()
        with self.assertRaises(RecoveryRequiredError):
            script.run()

    def test_unresolvable_reference_fails_before_second_call(self):
        calls = []
        script = Script("references")
        ref = script.add_function(lambda: {}, step_id="source")
        script.add_function(lambda item: calls.append(item), ref["missing"], step_id="target")
        with self.assertRaises(ResolutionError):
            script.run()
        self.assertEqual(calls, [])

    def test_concurrent_execution_and_mutation_rejected(self):
        entered, finish = ThreadEvent(), ThreadEvent()
        script = Script("busy")

        def blocking():
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("test synchronization timed out")

        script.add_function(blocking)
        errors = []

        def worker():
            try:
                script.run()
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(BusyError):
                script.run()
            with self.assertRaises(BusyError):
                script.add_pause(step_id="late")
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_events_redaction_and_observer_stop(self):
        script = Script("events")
        script.add_function(lambda data: data, "private-value", step_id="secret")
        console = StringIO()
        with redirect_stdout(console):
            script.run(console_log=True)
        self.assertNotIn("private-value", console.getvalue())
        calls, events = [], []
        script = Script("observer")
        script.add_function(lambda: calls.append(1), step_id="call")
        control = Control()

        def observer(event):
            events.append(event.kind)
            control.request_pause()

        self.assertEqual(script.run(control=control, observer=observer).next_step, 0)
        self.assertEqual(calls, [])
        self.assertEqual(events, ["starting", "paused"])

    def test_observer_failure_after_commit_does_not_replay(self):
        calls = []
        script = Script("observer")
        script.add_function(lambda: calls.append(1), step_id="call")

        def observer(event):
            if event.kind == "finished":
                raise ValueError("observer failed")

        with self.assertRaises(ValueError):
            script.run(observer=observer)
        self.assertEqual(script.run().status, Status.COMPLETED)
        self.assertEqual(calls, [1])

    def test_unsupported_pace(self):
        with self.assertRaisesRegex(ValueError, "line stepping"):
            Script("line").run(pace="line")

    def test_stop_during_marker_write_prevents_call_in_both_adapters(self):
        import asyncio
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from func_seq import CheckpointStore

        original = CheckpointStore.save
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), TemporaryDirectory() as directory:
                path = Path(directory) / "progress.json"
                control, calls = Control(), []
                script = Script("write-stop")
                script.add_function(lambda: calls.append(1), step_id="action")

                def save(store, data):
                    original(store, data)
                    if data["status"] == "running" and data["active_step"]:
                        control.request_pause()

                def execute():
                    options = dict(checkpoint=path, control=control)
                    return asyncio.run(script.arun(**options)) if asynchronous else script.run(**options)

                with patch.object(CheckpointStore, "save", save):
                    result = execute()
                self.assertEqual((result.status, result.next_step, calls), (Status.PAUSED, 0, []))
                self.assertIsNone(CheckpointStore(path).read()["active_step"])
                control.reset()
                self.assertEqual(execute().status, Status.COMPLETED)
                self.assertEqual(calls, [1])
