"""Named function queues and explicit references to preceding results."""

from dataclasses import dataclass
import hashlib
import inspect
import re
from threading import Lock
from typing import Any, Callable

from .errors import BusyError, DefinitionError, InvalidValueError, ResolutionError
from .values import MAX_DEPTH, copy_json, encode_json


def _identifier(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise DefinitionError("Names must be 1–128 ASCII letters, digits, dots, hyphens or underscores")
    return value


@dataclass(frozen=True)
class Ref:
    """A completed step's JSON result, optionally selected by key/index."""

    step_id: str
    path: tuple[str | int, ...] = ()

    def __post_init__(self):
        _identifier(self.step_id)
        if type(self.path) is not tuple or len(self.path) > MAX_DEPTH:
            raise DefinitionError("Reference path must be a bounded tuple")
        if any(type(part) not in (str, int) or (type(part) is int and part < 0)
               for part in self.path):
            raise DefinitionError("Reference paths use string keys or nonnegative indexes")

    def __getitem__(self, key: str | int) -> "Ref":
        return Ref(self.step_id, (*self.path, key))


def _copy_argument(value: Any, previous: set[str], depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        raise DefinitionError("Argument nesting exceeds the supported limit")
    if isinstance(value, Ref):
        if value.step_id not in previous:
            raise DefinitionError("References must point to earlier function steps")
        return value
    if type(value) is list:
        return [_copy_argument(item, previous, depth + 1) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise InvalidValueError("Argument object keys must be strings")
        return {key: _copy_argument(item, previous, depth + 1) for key, item in value.items()}
    return copy_json(value, depth)


def _describe(value: Any) -> dict:
    if isinstance(value, Ref):
        return {"ref": {"step": value.step_id, "path": list(value.path)}}
    if type(value) is list:
        return {"list": [_describe(item) for item in value]}
    if type(value) is dict:
        return {"dict": {key: _describe(item) for key, item in value.items()}}
    return {"value": value}


def resolve(value: Any, results: dict) -> Any:
    if isinstance(value, Ref):
        try:
            selected = results[value.step_id]
            for part in value.path:
                if type(part) is str and type(selected) is dict:
                    selected = selected[part]
                elif type(part) is int and type(selected) is list:
                    selected = selected[part]
                else:
                    raise KeyError
            return copy_json(selected)
        except (KeyError, IndexError):
            raise ResolutionError("Result reference cannot be resolved") from None
    if type(value) is list:
        return [resolve(item, results) for item in value]
    if type(value) is dict:
        return {key: resolve(item, results) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class _Step:
    step_id: str
    function: Callable | None
    action: str
    args: tuple
    kwargs: dict


class Script:
    """Build a sequential script. Bump version when callable behavior changes."""

    def __init__(self, name: str, *, version: str = "1") -> None:
        self._name = _identifier(name)
        self._version = _identifier(version)
        self._steps: list[_Step] = []
        self._lock = Lock()
        self._state = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self._steps)

    def add_function(self, function: Callable, /, *args,
                     step_id: str | None = None, **kwargs) -> Ref:
        if not callable(function):
            raise DefinitionError("Expected a callable")
        selected = step_id if step_id is not None else getattr(function, "__name__", None)
        selected = _identifier(selected)
        try:
            inspect.signature(function).bind(*args, **kwargs)
        except (TypeError, ValueError):
            raise DefinitionError("Function arguments do not match an inspectable signature") from None
        action = f"{getattr(function, '__module__', type(function).__module__)}:{getattr(function, '__qualname__', type(function).__qualname__)}"
        self._append(selected, function, action, args, kwargs)
        return Ref(selected)

    def add_pause(self, *, step_id: str) -> None:
        self._append(_identifier(step_id), None, "pause", (), {})

    def _append(self, step_id, function, action, args, kwargs):
        if not self._lock.acquire(blocking=False):
            raise BusyError("Script is executing")
        try:
            if self._state is not None:
                raise DefinitionError("A script with progress cannot be changed; build a new definition")
            if step_id in self.step_ids:
                raise DefinitionError("Step IDs must be unique")
            previous = {step.step_id for step in self._steps if step.function is not None}
            copied_args = tuple(_copy_argument(value, previous) for value in args)
            copied_kwargs = {key: _copy_argument(value, previous) for key, value in kwargs.items()}
            candidate = _Step(step_id, function, action, copied_args, copied_kwargs)
            self._steps.append(candidate)
            try:
                encode_json(self.manifest)
            except BaseException:
                self._steps.pop()
                raise
        finally:
            self._lock.release()

    @property
    def manifest(self) -> dict:
        """Return fresh passive metadata. No callables are reconstructed from it."""
        return {"schema": 1, "name": self.name, "version": self.version, "steps": [
            {"id": step.step_id, "action": step.action,
             "kind": "pause" if step.function is None else "function",
             "args": [_describe(value) for value in step.args],
             "kwargs": {key: _describe(value) for key, value in step.kwargs.items()}}
            for step in self._steps]}

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(encode_json(self.manifest)).hexdigest()
