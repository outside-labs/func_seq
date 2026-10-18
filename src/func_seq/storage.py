"""Passive bounded checkpoints on a caller-selected local filesystem path."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import UUID

from .errors import BusyError, CheckpointError, InvalidValueError
from .values import MAX_BYTES, decode_json, encode_json

_FIELDS = {"schema", "fingerprint", "run_id", "status", "cursor", "active_step",
           "results", "error_type"}
_STATUSES = {"ready", "running", "paused", "completed", "failed", "interrupted"}


def validate_document(data: dict) -> dict:
    """Validate the storage schema without importing any sequence code."""
    def require(condition):
        if not condition:
            raise CheckpointError("Checkpoint schema or progress is invalid")

    require(type(data) is dict and set(data) == _FIELDS)
    require(type(data["schema"]) is int and data["schema"] == 1)
    require(type(data["fingerprint"]) is str and
            re.fullmatch(r"[0-9a-f]{64}", data["fingerprint"]) is not None)
    require(type(data["run_id"]) is str)
    try:
        run_id = UUID(data["run_id"])
        require(str(run_id) == data["run_id"] and run_id.version == 4)
    except ValueError:
        raise CheckpointError("Checkpoint run identity is invalid") from None
    require(type(data["status"]) is str and data["status"] in _STATUSES)
    require(type(data["cursor"]) is int and data["cursor"] >= 0)
    require(type(data["results"]) is dict and len(data["results"]) == data["cursor"])
    require(all(type(key) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key)
                for key in data["results"]))
    active = data["active_step"]
    require(active is None or (type(active) is str and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", active) is not None))
    require(active is None or (active not in data["results"] and
            data["status"] in {"running", "failed", "interrupted"}))
    error = data["error_type"]
    if data["status"] in {"failed", "interrupted"}:
        require(active is not None and type(error) is str and 0 < len(error) <= 128
                and all(ord(char) >= 32 for char in error))
    else:
        require(error is None)
    if data["status"] == "ready":
        require(data["cursor"] == 0 and active is None)
    # Callers may pass raw data directly, so validate every value too.
    try:
        return decode_json(encode_json(data))
    except InvalidValueError:
        raise CheckpointError("Checkpoint contains invalid or excessive JSON data") from None


def _regular(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise CheckpointError("Checkpoint and lock paths must be regular files")
    return info


class CheckpointStore:
    """Explicit JSON storage. Parent directories must already exist."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path).absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._owned = False

    @contextmanager
    def owned(self):
        """Acquire exclusive ownership. Stale locks require manual inspection."""
        if self._owned:
            raise BusyError("Checkpoint is already owned")
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            raise BusyError("Checkpoint lock exists; verify its owner before removing it") from None
        except OSError as error:
            raise CheckpointError("Checkpoint lock could not be created") from error
        identity = os.fstat(fd)
        self._owned = True
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(f"pid={os.getpid()}\n".encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            yield self
        finally:
            self._owned = False
            try:
                current = _regular(self.lock_path)
                if current is None or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise CheckpointError("Checkpoint lock ownership changed")
                self.lock_path.unlink()
            except OSError as error:
                raise CheckpointError("Checkpoint lock could not be released") from error

    def read(self) -> dict | None:
        """Read passive metadata without importing code or creating a lock."""
        try:
            before = _regular(self.path)
            if before is None:
                return None
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode) or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                    raise CheckpointError("Checkpoint changed while opening it")
                payload = stream.read(MAX_BYTES + 1)
            return validate_document(decode_json(payload))
        except InvalidValueError:
            raise CheckpointError("Checkpoint is not valid bounded JSON") from None
        except OSError as error:
            raise CheckpointError("Checkpoint could not be read") from error

    def save(self, data: dict) -> None:
        if not self._owned:
            raise BusyError("Saving progress requires checkpoint ownership")
        payload = encode_json(validate_document(data))
        temporary = None
        try:
            _regular(self.path)
            fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Recheck before replacement; hostile directory mutation is out of scope.
            _regular(self.path)
            os.replace(temporary, self.path)
            temporary = None
            if os.name == "posix":
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as error:
            raise CheckpointError("Checkpoint could not be atomically saved") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self) -> bool:
        """Explicitly remove this progress file under exclusive ownership."""
        with self.owned():
            try:
                if _regular(self.path) is None:
                    return False
                self.path.unlink()
                return True
            except OSError as error:
                raise CheckpointError("Checkpoint could not be deleted") from error
