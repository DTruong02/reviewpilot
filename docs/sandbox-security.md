# Repository test sandbox: threat model and control boundary

## Scope

ReviewPilot currently parses reviewed source as data. There is no sandbox runner,
test-plan API, subprocess invocation, container integration, or code execution
path in the application. `backend/app/services/sandbox_policy.py` adds reusable
validation primitives for a possible later test-plan feature; it does not enable
execution. Do not connect it to a worker or endpoint until the external isolation
controls below exist and have been independently verified.

## Threat model

Repository files, pull request metadata, and any LLM-produced plan are untrusted.
An attacker may try to use a plan to select another repository or commit, inject
an arbitrary command, escape a checkout through path traversal or symlinks, or
exhaust resources with oversized evidence. A future executor must assume reviewed
code and its tests can execute arbitrary code with the process privileges it is
given.

## Enforced by the policy module

- Plans are data-only records with an exact field set. Unknown fields such as
  `command` are rejected; a plan selects only a named, preconfigured test
  template. The module never evaluates a plan or runs its `argv`.
- Repository names must match a configured exact owner/repository allowlist.
  Full commit SHA values are normalized and bound to the plan.
- Authorization requires an injected trusted approval verifier that authenticates
  an explicit approval matching the canonical plan hash, repository, SHA, and
  policy hash, and atomically consumes it to prevent replay. Authorization fails
  closed if that verifier is absent or rejects the approval.
- Test templates are immutable argument tuples, not shell strings. Shell
  interpreters and shell-command flags are rejected. Any future executor must
  invoke a fixed argument vector without a shell and must not append LLM input.
- Evidence item count, per-item excerpt size, total evidence bytes, path bytes,
  component length, and line span are bounded. Evidence lines must match source
  text at the declared path and range. Repository-relative paths reject absolute
  paths, drive syntax, backslashes, empty components, `.` and `..`.
  `resolve_repo_path` resolves existing symlinks and rejects paths that escape
  the resolved repository root.
- Authorization requires an injected trusted checkout verifier for every plan,
  even if it contains no evidence. It must return a sealed snapshot attestation
  binding the root to the allowlisted remote, approved full commit SHA, and tree
  digest. That attestation is included in the authorized plan for a future
  executor to use as its checkout identity.
- Authorization requires an injected trusted controls verifier; caller-provided
  `ExecutorControls` values by themselves cannot authorize anything. It fails
  closed if that verifier is absent or rejects the attestation.

## Not implemented or verified

Evidence reads stop at the requested line and share a 4 MB byte budget per
authorization. The approval, checkout, and controls verifier interfaces are
contracts only.
This module does not implement authenticated approval storage, expiry,
revocation, audit logging, approval identity, immutable checkout creation, or
measure executor controls. Integrators must provide those trusted services and
independently verify a container runtime, mount configuration, network policy,
secret isolation, identity, CPU/memory/time quotas, process and output limits,
and cleanup. It does not create an ephemeral workspace, copy or mount a
repository, enforce read-only source access, or run templates. No execution
authorization should be issued in production until those services exist and
have been independently verified.

The helper resolves paths at validation time; a future executor must prevent
time-of-check/time-of-use symlink or content changes. A future executor must
consume the exact sealed snapshot identified by the returned attestation, rather
than reopening a mutable path. The verifier authenticates approvals before
expensive validation, then atomically consumes them after the remaining checks
pass to prevent replay.
The current policy is not an operating-system sandbox and must not be described
as one.
