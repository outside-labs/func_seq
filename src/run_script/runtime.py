"""A shared sequential state machine with calls at explicit yield boundaries."""

from dataclasses import dataclass, replace
from enum import StrEnum
import inspect
from typing import Any, Callable
from uuid import uuid4

from .control import Control, _current
from .definitions import Script, resolve
from .errors import BusyError, DefinitionError, RecoveryRequiredError, StepFailedError
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
    script: str
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


def _result(script: Script, state: _State) -> RunResult:
    return RunResult(state.status, state.cursor, script.step_ids[:state.cursor],
                     copy_json(state.results), state.run_id)


def _options(pace, control, observer, console_log):
    if pace not in ("auto", "function", "pause"):
        raise ValueError("pace must be 'auto', 'function' or 'pause'; line stepping is unsupported")
    if control is not None and not isinstance(control, Control):
        raise TypeError("control must be a Control")
    if observer is not None and not callable(observer):
        raise TypeError("observer must be callable")
    if type(console_log) is not bool:
        raise TypeError("console_log must be bool")
    return control if control is not None else Control()


def _drive(script, state, persist, *, pace, control, observer, console_log, retry):
    def emit(kind, step_id=None):
        event = Event(kind, script.name, step_id, state.cursor)
        if console_log:
            print(f"{event.script}: {event.kind}" + (f" {step_id}" if step_id else ""))
        if observer is not None:
            observer(event)

    if state.active_step is not None:
        if not retry:
            raise RecoveryRequiredError("An uncertain step requires retry=True or restart=True")
        state = replace(state, active_step=None, status=Status.READY, error_type=None)
        persist(state)
    while state.cursor < len(script._steps):
        if control.pause_requested:
            state = replace(state, status=Status.PAUSED)
            persist(state)
            emit("paused")
            return _result(script, state)
        step = script._steps[state.cursor]
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
        if state.cursor < len(script._steps) and (
            control.pause_requested or (pace == "function" and step.function is not None)
            or (pace == "pause" and step.function is None)
        ):
            state = replace(state, status=Status.PAUSED)
            persist(state)
            emit("paused")
            return _result(script, state)
    state = replace(state, status=Status.COMPLETED)
    persist(state)
    emit("completed")
    return _result(script, state)


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


def run(script: Script, *, pace="auto", control=None, observer=None,
        console_log=False, restart=False, retry=False) -> RunResult:
    control = _options(pace, control, observer, console_log)
    if not script._lock.acquire(blocking=False):
        raise BusyError("Script is already executing")
    try:
        state = _State(script.fingerprint, str(uuid4())) if restart or script._state is None else script._state

        def persist(updated):
            script._state = updated

        persist(state)
        return _pump_sync(_drive(script, state, persist, pace=pace, control=control,
                                 observer=observer, console_log=console_log, retry=retry))
    finally:
        script._lock.release()
