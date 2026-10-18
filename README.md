# func_seq

Run ordered Python functions, pass JSON results forward, pause at function
boundaries, and resume from saved progress. Python 3.14+; no runtime dependencies.

Install with `pip install func-seq`. The import package is `func_seq`, and the
console command is `func-seq`.

## Start with a script

From the source checkout, run `uv sync`. Save this as `greeting.py`:

```python
from func_seq import Script

def collect():
    return {"message": "Hello from func_seq!"}

def display(message):
    print(message)
    return {"displayed": True}

script = Script("greeting", version="1")
collected = script.add_function(collect)
script.add_function(display, collected["message"])

if __name__ == "__main__":
    first = script.run(checkpoint="greeting.progress.json", pace="function")
    assert first.status == "paused"
    result = script.run(checkpoint="greeting.progress.json")
    assert result.results["display"] == {"displayed": True}
```

`add_function` queues a call and returns a `Ref` to its eventual result. It does
not execute the function. References select string dictionary keys or nonnegative
list indexes and must point to earlier function steps. Arguments can contain
references inside lists and dictionaries. `step_id="..."` overrides the default
function name; IDs must be unique. Wrap functions that need a keyword named
`step_id` to avoid the registration option of the same name.

Functions return strict JSON values: `None`, booleans, integers, finite floats,
Unicode strings, lists and dictionaries with string keys. Tuples, classes,
bytes, open files and native handles are rejected. Arguments and outputs are
copied so one step cannot mutate saved prior results. Missing result paths fail
before the dependent function runs.

## Pause and continue

| `pace` | Behavior |
| --- | --- |
| `auto` | Continue until completion, failure or an external pause request. |
| `function` | Complete at most one function, then return paused if work remains. |
| `pause` | Continue until `pause()` is called or a queued pause marker completes. |

`script.add_pause(step_id="review")` queues a visible marker. Its output is
`None`, and it cannot be used as a result reference. `from func_seq import pause`
provides `pause()` inside a function. It requests a pause after the function
returns, so subsequent lines still run. It does nothing outside a running script
or with another pace. Completion takes precedence if no work remains.

Pass `control=Control()` to either runner. An input adapter or another thread
may call `control.request_pause()`. The current function can finish, but another
function will not start. Call `control.reset()` before continuing with that same
control. Split long actions into smaller functions to improve stop latency.
There is no forced thread termination, line debugger or global keyboard listener.

Importing the library starts no threads, opens no desktop sessions, installs no
signal handlers and creates no files. `console_log=True` prints structural events;
a synchronous `observer(event)` receives `kind`, `script`, `step_id` and `next_step`.
Events contain no arguments or results. User functions are responsible for their
own prints and logs. Observer exceptions propagate; committed steps stay completed.

## Progress and recovery

An explicit `checkpoint=` enables durable progress in an existing parent directory.
Without it, progress lives only in that `Script` instance. Repeated calls continue;
a completed run returns saved results without repeating calls. Choose storage on
the first invocation and keep using it. To switch storage, build a fresh definition
or explicitly restart. Definitions cannot be changed after execution begins.

`RunResult` exposes `status`, `next_step` (the next queue index), `completed`
(step IDs), `results` (a copied dictionary keyed by step ID) and `run_id`.
Successful runner calls return paused or completed.

Progress is bounded, versioned JSON written atomically under an exclusive lock.
It contains no serialized callables and is never unpickled. A manifest fingerprint
detects changes to script name/version, step order, callable names and registered
arguments. **Bump `Script(..., version="2")` when function behavior, captured values,
defaults or relevant environment configuration changes.** The fingerprint does
not hash function source or prove an unchanged environment.

An in-flight marker is saved before a function begins. A crash, exception, invalid
return, cancellation or failed result write can leave the action uncertain.
Ordinary resume raises `RecoveryRequiredError`. Inspect external effects, then
choose `retry=True` to repeat that action or `restart=True` to discard progress
and repeat the whole script. These options are mutually exclusive.
`StepFailedError` exposes `step_id` and a chained cause. Interrupts/cancellation
propagate after recording the interrupted step. Corrupt or incompatible progress
always fails explicitly.

Retry and restart do not roll back effects or guarantee exactly-once execution.
Use application-level idempotency keys or external receipts when repetition matters.
`script.delete_progress()` removes its bound file and resets its memory;
`CheckpointStore(path).delete()` removes a selected file without executing code.
Deletion does not undo actions.

Hard termination may leave `<checkpoint>.lock`. Confirm the original process has
stopped before manually removing it. The runner does not infer safety from an old
PID or lock. See the [storage contract](docs/storage.md) for schema and limits.

## Asynchronous functions

```python
import asyncio
from func_seq import Script

async def collect():
    return {"value": 3}

def double(value):
    return value * 2

async def main():
    script = Script("async-example")
    collected = script.add_function(collect)
    script.add_function(double, collected["value"])
    result = await script.arun()
    assert result.results["double"] == 6

asyncio.run(main())
```

`arun()` awaits mixed steps sequentially. Synchronous functions and checkpoint
writes run on the calling thread; blocking work blocks the event loop. Steps
must own and await their work rather than leave detached tasks after completion.
Cancellation preserves uncertainty and propagates `asyncio.CancelledError`.
Use `await script.arun()` inside an existing event loop; `run()` rejects async
functions instead of creating another loop.

## Command line

Run the bundled example from the checkout:

```console
uv run func-seq run examples.greeting:script --checkpoint greeting.progress.json --interactive
uv run func-seq status greeting.progress.json
uv run func-seq run examples.greeting:script --checkpoint greeting.progress.json
uv run func-seq delete greeting.progress.json
```

`run` imports an explicitly selected **trusted** `module:attribute`. The current
directory is searched first, and the attribute must be a `Script`. Imports and
calls execute ordinary Python code; this command is not a sandbox. Use
`greeting:script` for your saved example. `status` and `delete` manage only data
and never import that script. CLI summaries omit stored outputs.

`--interactive` uses function pacing: Return continues; any text, EOF or Ctrl-C
at the prompt leaves progress paused. Without it, the command never reads stdin.
During execution Ctrl-C requests a cooperative pause and waits for the function
to return. The prior signal handler is restored afterward. A function that blocks
indefinitely cannot be forced to finish by cooperative STOP.

Use `--async` for async sequences, `--pace function` or `--pace pause` for a single
paced invocation, and `--console-log` for structural events. `--restart` and
`--retry` authorize repetition. Completion/cooperative pause returns exit code 0;
errors return 2; KeyboardInterrupt at the prompt returns 130.
`python -m func_seq` provides the same commands. The library intercepts no signals.

## Development

```console
uv run python -m unittest discover -s tests -v
uv build --out-dir dist
uv run python scripts/check_distribution.py dist
```

CI checks Python 3.14 on Linux, macOS and Windows, distribution contents and an
isolated installed CLI. Tests use synthetic functions and temporary files. There
are no real desktop actions. License selection, publication, desktop listeners
and recording formats retain separate decisions in the
[roadmap](https://github.com/outside-labs/func_seq/issues/1) and
[Project](https://github.com/orgs/outside-labs/projects/12).
