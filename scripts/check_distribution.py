"""Check distribution allowlists and execute an isolated installed package."""

import argparse
import base64
import csv
from email.parser import BytesParser
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

PACKAGE_FILES = {
    "__init__.py", "__main__.py", "cli.py", "control.py", "definitions.py",
    "errors.py", "py.typed", "runtime.py", "storage.py", "values.py",
}
SUPPORT_FILES = {
    "README.md", "pyproject.toml", "pyproject.toml.orig", "PKG-INFO",
    "docs/storage.md", "examples/__init__.py", "examples/greeting.py",
    "scripts/check_distribution.py",
    *{f"tests/test_{name}.py" for name in ("async", "cli", "definitions", "documentation", "runtime", "storage")},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(command, *, cwd, environment):
    result = subprocess.run([str(part) for part in command], cwd=cwd, env=environment,
                            capture_output=True, text=True, timeout=120)
    require(result.returncode == 0, f"Command failed: {command[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def check(directory):
    wheels, sources = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    require(len(wheels) == len(sources) == 1, "Use a directory containing exactly one wheel and one sdist")
    wheel, source = wheels[0].resolve(), sources[0].resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        require(len(names) == len(set(names)), "Duplicate wheel paths")
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata_paths) == 1, "Expected exactly one wheel metadata file")
        metadata_path = metadata_paths[0]
        info = metadata_path.rsplit("/", 1)[0]
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        require(metadata["Name"] == "func-seq" and metadata["Requires-Python"] == ">=3.14", "Unexpected metadata")
        require(not metadata.get_all("Requires-Dist"), "Runtime dependencies must remain empty")
        allowed = {f"func_seq/{name}" for name in PACKAGE_FILES} | {
            f"{info}/{name}" for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")}
        require(set(names) == allowed, "Wheel file allowlist mismatch")
        require("func-seq = func_seq.cli:main" in archive.read(f"{info}/entry_points.txt").decode(), "Console entry missing")
        package = {name: archive.read(f"func_seq/{name}") for name in PACKAGE_FILES}
        records = list(csv.reader(StringIO(archive.read(f"{info}/RECORD").decode())))
        require({row[0] for row in records} == allowed and len(records) == len(allowed), "Invalid RECORD paths")
        for path, digest, size in records:
            if path.endswith("/RECORD"):
                require(not digest and not size, "RECORD must omit its own hash")
            else:
                payload = archive.read(path)
                expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
                require(digest == expected and size == str(len(payload)), "Wheel RECORD hash mismatch")
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        require(all(member.isfile() or member.isdir() for member in members), "Unexpected sdist member type")
        paths = [member.name for member in members if member.isfile()]
        require(len(paths) == len(set(paths)), "Duplicate sdist paths")
        prefix = f"func_seq-{metadata['Version']}/"
        require(all(path.startswith(prefix) for path in paths), "Unexpected sdist root")
        contents = {path.removeprefix(prefix): archive.extractfile(path).read() for path in paths}
        require(set(contents) == SUPPORT_FILES | {f"src/func_seq/{name}" for name in PACKAGE_FILES}, "Sdist file allowlist mismatch")
        for name, payload in package.items():
            require(contents[f"src/func_seq/{name}"] == payload, "Wheel/source implementation mismatch")
        source_metadata = BytesParser().parsebytes(contents["PKG-INFO"])
        require(source_metadata["Version"] == metadata["Version"] and not source_metadata.get_all("Requires-Dist"), "Sdist metadata mismatch")
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="func-seq-distribution-") as temporary:
        root = Path(temporary)
        installation = root / "environment"
        venv.EnvBuilder(with_pip=True).create(installation)
        binary = installation / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        console = binary / ("func-seq.exe" if os.name == "nt" else "func-seq")
        working = root / "working"
        working.mkdir()
        run([python, "-m", "pip", "install", "--no-deps", "--no-index", wheel], cwd=working, environment=environment)
        smoke = 'from importlib.metadata import requires; from func_seq import Script; assert not requires("func-seq"); assert Script("smoke").run().status == "completed"'
        run([python, "-I", "-c", smoke], cwd=working, environment=environment)
        run([console, "--help"], cwd=working, environment=environment)
        run([python, "-I", "-m", "func_seq", "--help"], cwd=working, environment=environment)
        (working / "fixture.py").write_text('''from pathlib import Path
from func_seq import Script
def first():
    with Path("calls.txt").open("a") as stream: stream.write("first\\n")
    return {"value": "private-result"}
def second(value):
    with Path("calls.txt").open("a") as stream: stream.write("second\\n")
    return value
script = Script("installed")
ref = script.add_function(first)
script.add_function(second, ref["value"])
''')
        invocation = [console, "run", "fixture:script", "--checkpoint", "fixture.progress.json"]
        first = json.loads(run([*invocation, "--pace", "function"], cwd=working, environment=environment))
        require(first["status"] == "paused", "Installed function pacing failed")
        for _ in range(2):
            complete = json.loads(run(invocation, cwd=working, environment=environment))
            require(complete["status"] == "completed", "Installed resume failed")
        require((working / "calls.txt").read_text().splitlines() == ["first", "second"], "Installed resume replayed actions")
        status = run([console, "status", "fixture.progress.json"], cwd=working, environment=environment)
        require("private-result" not in status, "Status exposed saved outputs")
        run([console, "delete", "fixture.progress.json"], cwd=working, environment=environment)
        require(not (working / "fixture.progress.json").exists(), "Installed delete failed")
        extracted = root / "source"
        for path, payload in contents.items():
            destination = extracted / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        run([python, "-I", "-m", "unittest", "discover", "-s", extracted / "tests", "-v"], cwd=extracted, environment=environment)
    print("Distribution allowlists, hashes, source parity and isolated installed tests passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    check(parser.parse_args().directory)
