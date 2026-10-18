"""Cooperative Python scripts with explicit, serializable step boundaries."""

from .definitions import Ref, Script
from .control import Control, pause
from .errors import (
    BusyError, CheckpointError, DefinitionError, DefinitionMismatchError,
    InvalidValueError, RecoveryRequiredError,
    ResolutionError, ScriptError, StepFailedError,
)
from .runtime import Event, RunResult, Status
from .storage import CheckpointStore

__all__ = [
    "BusyError", "CheckpointError", "CheckpointStore", "Control", "DefinitionError",
    "DefinitionMismatchError", "Event", "InvalidValueError",
    "RecoveryRequiredError", "Ref", "ResolutionError", "RunResult", "Script",
    "ScriptError", "Status", "StepFailedError", "pause",
]


def main() -> None:
    print("run_script: define a Script to queue named Python functions")
