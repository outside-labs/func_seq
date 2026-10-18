# Local progress storage

The caller selects a local file in an existing parent directory. Keep that
directory private. JSON is plaintext and unauthenticated: schema validation
cannot stop a malicious local writer from fabricating plausible results. Avoid
storing secrets in results. Reading progress never imports script code.

## Schema version 1

| Field | Contract |
| --- | --- |
| `schema` | Integer 1; unknown versions and extra fields fail. |
| `fingerprint` | Lowercase SHA256 of the canonical definition manifest. |
| `run_id` | Canonical UUID4 retained across resumes. |
| `status` | `ready`, `running`, `paused`, `completed`, `failed`, `interrupted`. |
| `cursor` | Nonnegative integer indexing the next queued step. |
| `active_step` | Current uncertain step ID, otherwise null. |
| `results` | Exact completed prefix of IDs to copied JSON values. |
| `error_type` | Exception class name for failed/interrupted steps; otherwise null. |

No exception message, traceback, function object or reconstruction recipe is
stored. Results remain application data and can contain sensitive values.
Status inspection reports counts and failure metadata, excluding outputs.
Names are visible metadata and should not contain secrets.

Documents are limited to 4 MiB of UTF-8 and 64 levels of nesting. Parsing rejects
duplicate keys, non-finite numbers and invalid Unicode. The runner validates
the completed prefix, queue bounds, active-step identity and completed status
against the trusted definition. Pause-marker outputs must be null.

## Write boundaries

1. Resolve inputs and notify observers before invoking an action.
2. Persist running state with an active step.
3. Execute the function and validate its result.
4. Persist the result, advance the cursor and clear the active marker.
5. Persist paused or completed at the appropriate boundary.

Running state with no active step can occur between commits and is safe to
continue: the prior result is committed. Failed/interrupted states retain an
active step. Normal resume refuses every active step, including a running marker
left by process death. Retry explicitly clears uncertainty before repeating it;
the runner cannot determine what happened in an external application.

A failure before the active marker is saved never invokes the function. After
invocation, failure preserves that marker or a failed state. If the failure write
also fails, the pre-call marker remains. If only the terminal completed-status
write fails after committing an output, normal resume finalizes completion
without repeating the action. There are no automatic retries or rollbacks.

## Ownership and filesystem assumptions

An exclusive `<checkpoint>.lock` is created with mode 0600 and held through the
invocation. It contains a PID for inspection and is removed on normal exit,
exceptions and async cancellation. Hard exit may leave it. Independently confirm
its owner has stopped before manually removing it; no automatic stale-lock
breaking is attempted.

Each write uses a same-directory temporary file with restrictive permissions,
flush/fsync and atomic replacement. POSIX also fsyncs the directory. Temporary
files are cleaned on ordinary failure; process death can leave dot-prefixed
temporary files, which are never loaded as progress. Replacement adopts the
temporary file's permissions. Windows ACLs follow the parent directory's policy.

Progress/lock paths must be regular files, not symlinks. Reads check identity at
open time; lock release checks its ownership. These checks assume a trusted
parent directory. Hostile directory mutation, privileged writers, network
filesystem locks and filesystems without replacement/fsync support are outside
this storage contract. Use a local filesystem.

Restart explicitly replaces malformed or incompatible progress with a new run,
while retaining path and ownership checks. Deletion removes only the selected
file under the same lock. It never follows checkpoint-supplied paths, removes
directories or undoes actions.

Python's [pickle documentation](https://docs.python.org/3.14/library/pickle.html)
explains why object reconstruction is unsuitable for passive progress. Signing
pickle can authenticate a producer but cannot make reconstruction passive.
There is no pickle compatibility mode.
