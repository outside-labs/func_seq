from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from func_seq import CheckpointStore, Script
from func_seq.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def invoke(self, arguments, **kwargs):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = main(arguments)
        return result, out.getvalue(), err.getvalue()

    def test_status_and_delete_are_data_only_and_hide_outputs(self):
        script = Script("secret")
        script.add_function(lambda: "private-value", step_id="secret")
        script.run(checkpoint=self.path)
        with patch("func_seq.cli.import_module", side_effect=AssertionError("unexpected import")):
            code, out, _ = self.invoke(["status", str(self.path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["status"], "completed")
            self.assertNotIn("private-value", out)
            self.assertEqual(self.invoke(["delete", str(self.path)])[0], 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(json.loads(self.invoke(["status", str(self.path)])[1]), {"status": "missing"})

    def test_default_does_not_read_input_and_restores_signals(self):
        script = Script("default")
        script.add_function(lambda: 1, step_id="one")
        previous = signal.getsignal(signal.SIGINT)
        with patch("func_seq.cli._load_target", return_value=script), patch("builtins.input", side_effect=AssertionError):
            result = self.invoke(["run", "demo:script", "--checkpoint", str(self.path)])
        self.assertEqual(result[0], 0)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_interactive_eof_stop_and_keyboard_interrupt_preserve_progress(self):
        for response, expected in ((EOFError(), 0), (KeyboardInterrupt(), 130), ("stop", 0), ("other text", 0)):
            with self.subTest(response=type(response)):
                script = Script("interactive")
                script.add_function(lambda: 1, step_id="one")
                script.add_function(lambda: 2, step_id="two")
                side = dict(side_effect=response) if isinstance(response, BaseException) else dict(return_value=response)
                with patch("func_seq.cli._load_target", return_value=script), patch("builtins.input", **side):
                    code, _, _ = self.invoke(["run", "demo:script", "--checkpoint", str(self.path), "--interactive", "--restart"])
                self.assertEqual(code, expected)
                self.assertEqual(CheckpointStore(self.path).read()["cursor"], 1)

    def test_interactive_return_continues_once_without_restarting(self):
        calls = []
        script = Script("interactive")
        for index in range(2):
            script.add_function(lambda value: calls.append(value), index, step_id=f"step{index}")
        with patch("func_seq.cli._load_target", return_value=script), patch("builtins.input", return_value="") as prompt:
            code, _, _ = self.invoke(["run", "demo:script", "--checkpoint", str(self.path), "--interactive", "--restart"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 1])
        prompt.assert_called_once()

    def test_ctrl_c_during_function_requests_boundary_pause(self):
        script = Script("signal")
        calls = []

        def action():
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            calls.append("finished")

        script.add_function(action)
        script.add_function(lambda: calls.append("next"), step_id="next")
        with patch("func_seq.cli._load_target", return_value=script):
            code, out, _ = self.invoke(["run", "demo:script", "--checkpoint", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "paused")
        self.assertEqual(calls, ["finished"])

    def test_invalid_target_and_corrupt_progress_report_without_traceback(self):
        code, _, error = self.invoke(["run", "not-a-target", "--checkpoint", str(self.path)])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", error)
        self.path.write_text("private corrupt data")
        code, _, error = self.invoke(["status", str(self.path)])
        self.assertEqual(code, 2)
        self.assertNotIn("private", error)

    def test_module_mode_and_async_target_from_current_directory(self):
        module = Path(self.directory.name) / "fixture_script.py"
        module.write_text('''from func_seq import Script
async def action(): return {"value": 1}
script = Script("fixture")
script.add_function(action)
''')
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        process = subprocess.run([sys.executable, "-m", "func_seq", "run", "fixture_script:script",
                                  "--checkpoint", str(self.path), "--async"], cwd=self.directory.name,
                                 env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "completed")
