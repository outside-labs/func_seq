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
