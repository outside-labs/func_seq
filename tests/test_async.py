import asyncio
from pathlib import Path
import tempfile
import unittest

from run_script import (
    BusyError, CheckpointStore, Control, DefinitionError, RecoveryRequiredError,
    Script, Status, StepFailedError, pause,
)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_steps_and_references_execute_sequentially(self):
        calls = []
        script = Script("mixed")

        async def first():
            calls.append("first")
            return {"items": [2]}

        def second(value):
            calls.append("second")
            return value * 3

        ref = script.add_function(first)
        script.add_function(second, ref["items"][0])
        result = await script.arun()
        self.assertEqual(result.results["second"], 6)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(await script.arun(), result)

    async def test_cancellation_is_durable_and_explicitly_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            entered, finish = asyncio.Event(), asyncio.Event()
            calls = []

            async def action():
                calls.append(1)
                entered.set()
                await finish.wait()
                return "done"

            def build():
                script = Script("cancellation")
                script.add_function(action)
                return script

            task = asyncio.create_task(build().arun(checkpoint=path))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(CheckpointStore(path).read()["status"], "interrupted")
            self.assertFalse(CheckpointStore(path).lock_path.exists())
            with self.assertRaises(RecoveryRequiredError):
                await build().arun(checkpoint=path)
            finish.set()
            result = await build().arun(checkpoint=path, retry=True)
            self.assertEqual(result.status, Status.COMPLETED)
            self.assertEqual(calls, [1, 1])

    async def test_stop_waits_for_awaited_function_to_finish(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        control, calls = Control(), []
        script = Script("stop")

        async def first():
            entered.set()
            await finish.wait()
            calls.append("first-finished")

        script.add_function(first)
        script.add_function(lambda: calls.append("second"), step_id="second")
        task = asyncio.create_task(script.arun(control=control))
        await asyncio.wait_for(entered.wait(), 5)
        control.request_pause()
        finish.set()
        result = await task
        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(calls, ["first-finished"])
        control.reset()
        self.assertEqual((await script.arun(control=control)).status, Status.COMPLETED)

    async def test_same_script_execution_and_mutation_are_excluded(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        script = Script("busy")

        async def action():
            entered.set()
            await finish.wait()

        script.add_function(action)
        task = asyncio.create_task(script.arun())
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with self.assertRaises(BusyError):
                await script.arun()
            with self.assertRaises(BusyError):
                script.add_pause(step_id="late")
        finally:
            finish.set()
            await task

    async def test_independent_scripts_have_isolated_pause_contexts(self):
        first_entered, second_entered = asyncio.Event(), asyncio.Event()
        one, two = Script("one"), Script("two")

        async def first():
            first_entered.set()
            await second_entered.wait()
            pause()

        async def second():
            second_entered.set()
            await first_entered.wait()

        one.add_function(first)
        two.add_function(second)
        for script in (one, two):
            script.add_function(lambda: 1, step_id="last")
        a, b = await asyncio.gather(one.arun(pace="pause"), two.arun(pace="pause"))
        self.assertEqual((a.status, b.status), (Status.PAUSED, Status.COMPLETED))
        control = Control()
        await Script("empty").arun(control=control, pace="pause")
        pause()
        self.assertFalse(control.pause_requested)

    async def test_sync_adapter_rejects_known_async_before_any_calls(self):
        calls = []
        script = Script("wrong-adapter")
        script.add_function(lambda: calls.append(1), step_id="first")

        async def action():
            return 1

        script.add_function(action)
        with self.assertRaisesRegex(DefinitionError, "arun"):
            script.run()
        self.assertEqual(calls, [])
        self.assertEqual((await script.arun()).status, Status.COMPLETED)

    async def test_sync_adapter_closes_unexpected_coroutine(self):
        async def coroutine():
            return 1

        script = Script("dynamic")
        script.add_function(lambda: coroutine(), step_id="factory")
        with self.assertRaises(StepFailedError) as error:
            script.run()
        self.assertIn("arun", str(error.exception.__cause__))
        self.assertEqual((await script.arun(retry=True)).status, Status.COMPLETED)

    async def test_async_observers_are_rejected_without_calling_step(self):
        calls = []
        script = Script("observer")
        script.add_function(lambda: calls.append(1), step_id="first")

        async def observer(event):
            return None

        with self.assertRaises(TypeError):
            await script.arun(observer=observer)
        with self.assertRaises(TypeError):
            await script.arun(observer=lambda event: observer(event))
        self.assertEqual(calls, [])
        self.assertEqual((await script.arun()).status, Status.COMPLETED)
