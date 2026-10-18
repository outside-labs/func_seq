"""Public errors without automatic inclusion of application values."""


class ScriptError(Exception):
    """Base error for script definition and execution."""


class DefinitionError(ScriptError, ValueError):
    """The queue cannot be executed as defined."""


class InvalidValueError(ScriptError, ValueError):
    """A value is not bounded, strict JSON data."""


class ResolutionError(ScriptError, ValueError):
    """An earlier result does not contain the requested path."""


class BusyError(ScriptError):
    """The script or checkpoint is already in use."""


class CheckpointError(ScriptError):
    """Progress could not be read, validated or safely stored."""


class DefinitionMismatchError(CheckpointError):
    """Progress belongs to a different script definition."""


class RecoveryRequiredError(ScriptError):
    """An uncertain step requires explicit retry or restart."""


class StepFailedError(ScriptError):
    """A callable or its result could not be committed."""

    def __init__(self, step_id: str):
        self.step_id = step_id
        super().__init__(f"Step '{step_id}' did not complete; explicit recovery is required")
