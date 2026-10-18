"""Public errors without automatic inclusion of application values."""


class SequenceError(Exception):
    """Base error for sequence definition and execution."""


class DefinitionError(SequenceError, ValueError):
    """The queue cannot be executed as defined."""


class InvalidValueError(SequenceError, ValueError):
    """A value is not bounded, strict JSON data."""


class ResolutionError(SequenceError, ValueError):
    """An earlier result does not contain the requested path."""


class BusyError(SequenceError):
    """The sequence or checkpoint is already in use."""


class CheckpointError(SequenceError):
    """Progress could not be read, validated or safely stored."""


class DefinitionMismatchError(CheckpointError):
    """Progress belongs to a different sequence definition."""


class RecoveryRequiredError(SequenceError):
    """An uncertain step requires explicit retry or restart."""


class StepFailedError(SequenceError):
    """A callable or its result could not be committed."""

    def __init__(self, step_id: str):
        self.step_id = step_id
        super().__init__(f"Step '{step_id}' did not complete; explicit recovery is required")
