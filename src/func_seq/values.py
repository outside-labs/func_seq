"""Bounded passive JSON values; no object reconstruction or implicit coercion."""

import json
import math
from typing import Any

from .errors import InvalidValueError

MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64


def copy_json(value: Any, depth: int = 0) -> Any:
    """Copy exact JSON types, rejecting custom objects and excessive nesting."""
    if depth > MAX_DEPTH:
        raise InvalidValueError("JSON nesting exceeds the supported limit")
    kind = type(value)
    if value is None or kind in (bool, int):
        return value
    if kind is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise InvalidValueError("JSON text must be valid Unicode") from None
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind is list:
        return [copy_json(item, depth + 1) for item in value]
    if kind is dict:
        if any(type(key) is not str for key in value):
            raise InvalidValueError("JSON object keys must be strings")
        return {copy_json(key, depth + 1): copy_json(item, depth + 1)
                for key, item in value.items()}
    raise InvalidValueError("Expected strict JSON data")


def encode_json(value: Any) -> bytes:
    copied = copy_json(value)
    try:
        payload = json.dumps(copied, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidValueError("JSON value cannot be encoded") from None
    if len(payload) > MAX_BYTES:
        raise InvalidValueError("JSON document exceeds the supported size")
    return payload


def decode_json(payload: bytes) -> Any:
    if len(payload) > MAX_BYTES:
        raise InvalidValueError("JSON document exceeds the supported size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def constant(_value):
        raise InvalidValueError("Non-finite JSON number")

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant)
        # Also rejects floats that overflow to infinity, and unpaired surrogates.
        return copy_json(value)
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidValueError("Invalid bounded JSON document") from None
