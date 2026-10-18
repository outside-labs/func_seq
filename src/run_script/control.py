"""Opt-in cooperative controls; importing installs no input or signal hooks."""

from contextvars import ContextVar
from threading import Event


class Control:
    """A pause request that input adapters may set from another thread."""

    def __init__(self) -> None:
        self._pause = Event()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    def request_pause(self) -> None:
        self._pause.set()

    def reset(self) -> None:
        self._pause.clear()


_current: ContextVar[tuple[Control, str] | None] = ContextVar("run_script_control", default=None)


def pause() -> None:
    """Request a pause after this function, only while running with pace='pause'."""
    current = _current.get()
    if current is not None and current[1] == "pause":
        current[0].request_pause()
