"""Explicit trusted-code invocation and passive checkpoint management."""

import argparse
import asyncio
from contextlib import contextmanager
from importlib import import_module
from importlib.metadata import version
import json
from pathlib import Path
import re
import signal
import sys
from threading import current_thread, main_thread

from .control import Control
from .definitions import Script
from .errors import DefinitionError, ScriptError
from .runtime import Status
from .storage import CheckpointStore


def _parser():
    parser = argparse.ArgumentParser(prog="run-script", description="Run trusted Python steps with JSON progress.")
    parser.add_argument("--version", action="version", version=version("run-script"))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="import an explicitly selected module:script")
    run.add_argument("target", help="trusted module:attribute containing a Script")
    run.add_argument("--checkpoint", required=True, help="progress file in an existing local directory")
    run.add_argument("--pace", choices=("auto", "function", "pause"), default="auto")
    run.add_argument("--async", dest="asynchronous", action="store_true", help="await asynchronous steps")
    run.add_argument("--interactive", action="store_true", help="prompt after each function; Return continues")
    run.add_argument("--console-log", action="store_true", help="print structural events without values")
    recovery = run.add_mutually_exclusive_group()
    recovery.add_argument("--restart", action="store_true", help="discard progress and repeat the entire script")
    recovery.add_argument("--retry", action="store_true", help="permit repetition of the uncertain current step")
    status = commands.add_parser("status", help="inspect progress without importing script code")
    status.add_argument("checkpoint")
    delete = commands.add_parser("delete", help="explicitly remove only the selected progress file")
    delete.add_argument("checkpoint")
    return parser


def _load_target(target):
    module, separator, attribute = target.partition(":")
    if not separator or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) or not attribute.isidentifier():
        raise DefinitionError("Target must be a trusted module:attribute")
    try:
        script = getattr(import_module(module), attribute)
    except Exception as error:
        raise DefinitionError(f"Trusted script module could not be loaded ({type(error).__name__})") from error
    if not isinstance(script, Script):
        raise DefinitionError("Selected attribute must be a Script")
    return script


@contextmanager
def _module_path():
    # Console entry points otherwise omit the current directory from sys.path.
    directory = str(Path.cwd())
    sys.path.insert(0, directory)
    try:
        yield
    finally:
        sys.path.remove(directory)


@contextmanager
def _cooperative_sigint(control):
    installed = current_thread() is main_thread()
    if installed:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _number, _frame: control.request_pause())
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


def _summary(data):
    if data is None:
        return {"status": "missing"}
    return {"status": data["status"], "next_step": data["cursor"],
            "completed_count": len(data["results"]), "active_step": data["active_step"],
            "error_type": data["error_type"], "run_id": data["run_id"]}


def _run(args):
    script = _load_target(args.target)
    control = Control()
    first = True
    while True:
        options = dict(checkpoint=args.checkpoint, pace="function" if args.interactive else args.pace,
                       control=control, console_log=args.console_log,
                       restart=args.restart and first, retry=args.retry and first)
        with _cooperative_sigint(control):
            result = asyncio.run(script.arun(**options)) if args.asynchronous else script.run(**options)
        first = False
        print(json.dumps({"status": str(result.status), "next_step": result.next_step,
                          "completed_count": len(result.completed), "run_id": result.run_id}))
        if result.status == Status.COMPLETED or not args.interactive or control.pause_requested:
            return 0
        try:
            reply = input("Return to continue; any text stops: ")
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print("\nProgress paused.")
            return 130
        if reply.strip():
            return 0


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(_summary(CheckpointStore(args.checkpoint).read())))
            return 0
        if args.command == "delete":
            removed = CheckpointStore(args.checkpoint).delete()
            print("Progress deleted." if removed else "No progress file.")
            return 0
        if args.interactive and args.pace != "auto":
            parser.error("--interactive supplies function pacing; omit --pace")
        with _module_path():
            return _run(args)
    except KeyboardInterrupt:
        print("Interrupted; inspect progress before continuing.", file=sys.stderr)
        return 130
    except (ScriptError, ValueError, TypeError, OSError) as error:
        print(f"run-script: {error}", file=sys.stderr)
        return 2
