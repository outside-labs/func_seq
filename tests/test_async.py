import asyncio
from pathlib import Path
import tempfile
import unittest

from func_seq import (
    BusyError, CheckpointStore, Control, DefinitionError, RecoveryRequiredError,
    Sequence, Status, StepFailedError, pause,
)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_steps_and_references_execute_sequentially(self):
        calls = []
        sequence = Sequence("mixed")

        async def first():
            calls.append("first")
            return {"items": [2]}

        def second(value):
            calls.append("second")
            return value * 3

        ref = sequence.add_function(first)
        sequence.add_function(second, ref["items"][0])
        result = await sequence.arun()
        self.assertEqual(result.results["second"], 6)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(await sequence.arun(), result)

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
                sequence = Sequence("cancellation")
                sequence.add_function(action)
                return sequence

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
        sequence = Sequence("stop")

        async def first():
            entered.set()
            await finish.wait()
            calls.append("first-finished")

        sequence.add_function(first)
        sequence.add_function(lambda: calls.append("second"), step_id="second")
        task = asyncio.create_task(sequence.arun(control=control))
        await asyncio.wait_for(entered.wait(), 5)
        control.request_pause()
        finish.set()
        result = await task
        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(calls, ["first-finished"])
        control.reset()
        self.assertEqual((await sequence.arun(control=control)).status, Status.COMPLETED)

    async def test_same_script_execution_and_mutation_are_excluded(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        sequence = Sequence("busy")

        async def action():
            entered.set()
            await finish.wait()

        sequence.add_function(action)
        task = asyncio.create_task(sequence.arun())
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with self.assertRaises(BusyError):
                await sequence.arun()
            with self.assertRaises(BusyError):
                sequence.add_pause(step_id="late")
        finally:
            finish.set()
            await task

    async def test_independent_scripts_have_isolated_pause_contexts(self):
        first_entered, second_entered = asyncio.Event(), asyncio.Event()
        one, two = Sequence("one"), Sequence("two")

        async def first():
            first_entered.set()
            await second_entered.wait()
            pause()

        async def second():
            second_entered.set()
            await first_entered.wait()

        one.add_function(first)
        two.add_function(second)
        for sequence in (one, two):
            sequence.add_function(lambda: 1, step_id="last")
        a, b = await asyncio.gather(one.arun(pace="pause"), two.arun(pace="pause"))
        self.assertEqual((a.status, b.status), (Status.PAUSED, Status.COMPLETED))
        control = Control()
        await Sequence("empty").arun(control=control, pace="pause")
        pause()
        self.assertFalse(control.pause_requested)

    async def test_sync_adapter_rejects_known_async_before_any_calls(self):
        calls = []
        sequence = Sequence("wrong-adapter")
        sequence.add_function(lambda: calls.append(1), step_id="first")

        async def action():
            return 1

        sequence.add_function(action)
        with self.assertRaisesRegex(DefinitionError, "arun"):
            sequence.run()
        self.assertEqual(calls, [])
        self.assertEqual((await sequence.arun()).status, Status.COMPLETED)

    async def test_sync_adapter_closes_unexpected_coroutine(self):
        async def coroutine():
            return 1

        sequence = Sequence("dynamic")
        sequence.add_function(lambda: coroutine(), step_id="factory")
        with self.assertRaises(StepFailedError) as error:
            sequence.run()
        self.assertIn("arun", str(error.exception.__cause__))
        self.assertEqual((await sequence.arun(retry=True)).status, Status.COMPLETED)

    async def test_async_observers_are_rejected_without_calling_step(self):
        calls = []
        sequence = Sequence("observer")
        sequence.add_function(lambda: calls.append(1), step_id="first")

        async def observer(event):
            return None

        with self.assertRaises(TypeError):
            await sequence.arun(observer=observer)
        with self.assertRaises(TypeError):
            await sequence.arun(observer=lambda event: observer(event))
        self.assertEqual(calls, [])
        self.assertEqual((await sequence.arun()).status, Status.COMPLETED)
