"""Cooperative Python scripts with explicit, serializable step boundaries."""

from .definitions import Ref, Script
from .errors import DefinitionError, InvalidValueError, ResolutionError, ScriptError

__all__ = [
    "DefinitionError", "InvalidValueError", "Ref", "ResolutionError", "Script",
    "ScriptError",
]


def main() -> None:
    print("run_script: define a Script to queue named Python functions")
