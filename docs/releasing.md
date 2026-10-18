# Releases

The distribution is `func-seq`, the Python import is `func_seq`, the builder is
`Script`, and the console entry point is `func-seq`. Version 0.1.0 is the first
release; the earlier development name has no published compatibility package.

## Candidate verification

Use Python 3.14 and the committed lockfile:

```console
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
uv build --no-sources
uv run --locked python scripts/check_distribution.py dist --tag v0.1.0
uvx --from twine twine check --strict dist/*
```

The distribution check validates the source/wheel allowlists, metadata identity,
empty runtime dependency list, RECORD hashes, source parity and release tag.
It installs the wheel into a new environment, runs the API/CLI outside the source
checkout, proves resume does not repeat completed calls, then runs all tests
from the source archive against the installed package. The wheel is built from
the source archive. CI also checks Python 3.14 on Linux, macOS and Windows.

## Publication

1. Merge the reviewed release preparation and require passing final-main CI.
2. Create an immutable `v<version>` tag on that exact commit.
3. Publish the GitHub release for that tag. This starts `release.yaml`.
4. The build job verifies the tag/version, builds and tests the distributions,
   checks metadata, and saves the exact artifacts. It has no publishing token.
5. The publish job downloads those artifacts and uses PyPI Trusted Publishing
   through the `pypi` environment. Only this job has `id-token: write`.
6. Verify both PyPI SHA256 digests against the workflow artifacts, perform a fresh
   registry install, and attach the verified distributions to the GitHub release.

The PyPI publisher must identify owner `outside-labs`, repository `func_seq`,
workflow `release.yaml`, and environment `pypi`. The GitHub environment permits
version tags. No long-lived PyPI API token is stored in this repository.
See [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

Manual `workflow_dispatch` runs verify a candidate and save artifacts without
publishing. Publishing is enabled only for a published stable GitHub release.
Do not move a released tag or overwrite artifacts; use a new version for changes.
If publication fails, inspect the job before retrying. If a partial upload exists,
confirm the existing files match the reviewed artifacts before taking any action.
