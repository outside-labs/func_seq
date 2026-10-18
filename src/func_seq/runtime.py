"""A shared sequential state machine with calls at explicit yield boundaries."""

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
import inspect
from typing import Any, Callable
from uuid import uuid4

from .control import Control, _current
from .definitions import Sequence, resolve
from .errors import (
    BusyError, CheckpointError, DefinitionError, DefinitionMismatchError,
    RecoveryRequiredError, StepFailedError,
)
from .storage import CheckpointStore
from .values import copy_json, encode_json


class Status(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class RunResult:
    status: Status
    next_step: int
    completed: tuple[str, ...]
    results: dict[str, Any]
    run_id: str


@dataclass(frozen=True)
class Event:
    kind: str
    sequence: str
    step_id: str | None
    next_step: int


@dataclass(frozen=True)
class _State:
    fingerprint: str
    run_id: str
    status: Status = Status.READY
    cursor: int = 0
    active_step: str | None = None
    results: dict | None = None
    error_type: str | None = None

    def __post_init__(self):
        if self.results is None:
            object.__setattr__(self, "results", {})


@dataclass(frozen=True)
class _Call:
    function: Callable
    args: tuple
    kwargs: dict


def _result(sequence: Sequence, state: _State) -> RunResult:
    return RunResult(state.status, state.cursor, sequence.step_ids[:state.cursor],
                     copy_json(state.results), state.run_id)


def _options(pace, control, observer, console_log):
    if pace not in ("auto", "function", "pause"):
        raise ValueError("pace must be 'auto', 'function' or 'pause'; line stepping is unsupported")
    if control is not None and not isinstance(control, Control):
        raise TypeError("control must be a Control")
    if observer is not None and not callable(observer):
        raise TypeError("observer must be callable")
    if observer is not None and inspect.iscoroutinefunction(observer):
        raise TypeError("observer must be synchronous")
    if type(console_log) is not bool:
        raise TypeError("console_log must be bool")
    return control if control is not None else Control()


def _drive(sequence, state, persist, *, pace, control, observer, console_log, retry):
    def emit(kind, step_id=None):
        event = Event(kind, sequence.name, step_id, state.cursor)
        if console_log:
            print(f"{event.sequence}: {event.kind}" + (f" {step_id}" if step_id else ""))
        if observer is not None:
            observed = observer(event)
            if inspect.isawaitable(observed):
                if inspect.iscoroutine(observed):
                    observed.close()
                raise TypeError("observer must be synchronous")

    if state.active_step is not None:
        if not retry:
            raise RecoveryRequiredError("An uncertain step requires retry=True or restart=True")
        state = replace(state, active_step=None, status=Status.PAUSED, error_type=None)
        persist(state)
    while state.cursor < len(sequence._steps):
        if control.pause_requested:
            state = replace(state, status=Status.PAUSED)
            persist(state)
            emit("paused")
            return _result(sequence, state)
        step = sequence._steps[state.cursor]
        # Resolve and notify before creating uncertainty about an external call.
        args = tuple(resolve(value, state.results) for value in step.args)
        kwargs = {key: resolve(value, state.results) for key, value in step.kwargs.items()}
        emit("starting", step.step_id)
        # Observers may also request STOP; honor it before the callable starts.
        if control.pause_requested:
            continue
        active = replace(state, status=Status.RUNNING, active_step=step.step_id, error_type=None)
        persist(active)
        state = active
        # A disk write may take long enough for STOP to arrive after the first
        # check. No callable has run yet, so this is still a safe pause boundary.
        if control.pause_requested:
            state = replace(state, status=Status.PAUSED, active_step=None)
            persist(state)
            emit("paused")
            return _result(sequence, state)
        try:
            token = _current.set((control, pace))
            try:
                output = None if step.function is None else (yield _Call(step.function, args, kwargs))
            finally:
                _current.reset(token)
            encode_json(output)
            committed = replace(state, cursor=state.cursor + 1, active_step=None,
                                results={**state.results, step.step_id: copy_json(output)})
            persist(committed)
            state = committed
        except BaseException as error:
            failed = replace(state, status=Status.FAILED if isinstance(error, Exception)
                             else Status.INTERRUPTED, error_type=type(error).__name__[:128])
            try:
                persist(failed)
            except BaseException:
                # The pre-call marker still records uncertainty if storage fails.
                pass
            if not isinstance(error, Exception):
                raise
            raise StepFailedError(step.step_id) from error
        emit("finished", step.step_id)
        if state.cursor < len(sequence._steps) and (
            control.pause_requested or (pace == "function" and step.function is not None)
            or (pace == "pause" and step.function is None)
        ):
            state = replace(state, status=Status.PAUSED)
            persist(state)
            emit("paused")
            return _result(sequence, state)
    state = replace(state, status=Status.COMPLETED)
    persist(state)
    emit("completed")
    return _result(sequence, state)


def _call_sync(call):
    output = call.function(*call.args, **call.kwargs)
    if inspect.isawaitable(output):
        if inspect.iscoroutine(output):
            output.close()
        raise DefinitionError("Synchronous run cannot await this step; use arun()")
    return output


def _pump_sync(driver):
    try:
        call = next(driver)
        while True:
            try:
                output = _call_sync(call)
            except BaseException as error:
                call = driver.throw(error)
            else:
                call = driver.send(output)
    except StopIteration as done:
        return done.value
    finally:
        driver.close()


def _document(state):
    return {"schema": 1, "fingerprint": state.fingerprint, "run_id": state.run_id,
            "status": str(state.status), "cursor": state.cursor,
            "active_step": state.active_step, "results": state.results,
            "error_type": state.error_type}


def _restore(sequence, data):
    if data["fingerprint"] != sequence.fingerprint:
        raise DefinitionMismatchError("Checkpoint belongs to a different sequence definition")
    cursor = data["cursor"]
    if cursor > len(sequence._steps) or set(data["results"]) != set(sequence.step_ids[:cursor]):
        raise CheckpointError("Checkpoint outputs are not the completed prefix")
    if data["active_step"] is not None and (
        cursor == len(sequence._steps) or data["active_step"] != sequence.step_ids[cursor]
    ):
        raise CheckpointError("Checkpoint active step is inconsistent")
    if data["status"] == "completed" and cursor != len(sequence._steps):
        raise CheckpointError("Checkpoint completion is inconsistent")
    if any(step.function is None and data["results"][step.step_id] is not None
           for step in sequence._steps[:cursor]):
        raise CheckpointError("Pause marker output must be null")
    return _State(data["fingerprint"], data["run_id"], Status(data["status"]), cursor,
                  data["active_step"], data["results"], data["error_type"])


@contextmanager
def _session(sequence, checkpoint, restart, retry):
    if type(restart) is not bool or type(retry) is not bool or (restart and retry):
        raise ValueError("restart and retry must be booleans and cannot both be true")
    if not sequence._lock.acquire(blocking=False):
        raise BusyError("Sequence is already executing")
    try:
        store = CheckpointStore(checkpoint) if checkpoint is not None else None
        selected = str(store.path) if store is not None else None
        if sequence._state is not None and sequence._checkpoint_path != selected and not restart:
            raise DefinitionError("Use a fresh Sequence or restart=True to select different progress storage")
        with store.owned() if store is not None else nullcontext():
            state = None if restart else sequence._state
            if store is not None and not restart:
                data = store.read()
                if data is None and sequence._state is not None:
                    raise CheckpointError("Progress disappeared; explicit restart is required")
                state = _restore(sequence, data) if data is not None else None
            if state is None:
                state = _State(sequence.fingerprint, str(uuid4()))

            def persist(updated):
                if store is not None:
                    store.save(_document(updated))
                sequence._state = updated
                sequence._checkpoint_path = selected

            persist(state)
            yield state, persist
    finally:
        sequence._lock.release()


def run(sequence: Sequence, *, pace="auto", control=None, observer=None,
        console_log=False, restart=False, retry=False, checkpoint=None) -> RunResult:
    control = _options(pace, control, observer, console_log)
    if any(inspect.iscoroutinefunction(step.function) for step in sequence._steps):
        raise DefinitionError("Sequence contains asynchronous functions; use arun()")
    with _session(sequence, checkpoint, restart, retry) as (state, persist):
        return _pump_sync(_drive(sequence, state, persist, pace=pace, control=control,
                                 observer=observer, console_log=console_log, retry=retry))


async def _pump_async(driver):
    try:
        call = next(driver)
        while True:
            try:
                output = call.function(*call.args, **call.kwargs)
                if inspect.isawaitable(output):
                    output = await output
            except BaseException as error:
                call = driver.throw(error)
            else:
                call = driver.send(output)
    except StopIteration as done:
        return done.value
    finally:
        driver.close()


async def arun(sequence: Sequence, *, pace="auto", control=None, observer=None,
               console_log=False, restart=False, retry=False, checkpoint=None) -> RunResult:
    """Await a sequential queue; synchronous functions execute on the calling thread."""
    control = _options(pace, control, observer, console_log)
    with _session(sequence, checkpoint, restart, retry) as (state, persist):
        return await _pump_async(_drive(sequence, state, persist, pace=pace, control=control,
                                       observer=observer, console_log=console_log, retry=retry))


def delete_progress(sequence, checkpoint=None):
    if not sequence._lock.acquire(blocking=False):
        raise BusyError("Sequence is already executing")
    try:
        selected = checkpoint if checkpoint is not None else sequence._checkpoint_path
        removed = CheckpointStore(selected).delete() if selected is not None else sequence._state is not None
        if selected is None or str(CheckpointStore(selected).path) == sequence._checkpoint_path:
            sequence._state = None
            sequence._checkpoint_path = None
        return removed
    finally:
        sequence._lock.release()
