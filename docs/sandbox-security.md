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
- Authorization requires an explicit approval record matching the canonical
  plan hash, repository, SHA, and policy hash. A changed plan or policy requires
  a new approval.
- Test templates are immutable argument tuples, not shell strings. Shell
  interpreters and shell-command flags are rejected. Any future executor must
  invoke a fixed argument vector without a shell and must not append LLM input.
- Evidence item count, per-item excerpt size, total excerpt size, and line span
  are bounded. Repository-relative paths reject absolute paths, drive syntax,
  backslashes, empty components, `.` and `..`. `resolve_repo_path` resolves
  existing symlinks and rejects paths that escape the resolved repository root.
- Authorization fails closed when approval or any executor-control assertion
  is absent. The control fields default to false.

## Not implemented or verified

The `ExecutorControls` values are caller-provided attestations, not measurements
or cryptographic proof. This module does not verify a container runtime, mount
configuration, network policy, secret isolation, user identity, CPU/memory/time
quotas, process limits, output limits, or cleanup. It does not create an
ephemeral workspace, copy or mount a repository, enforce read-only source access,
or run templates. No execution authorization should be issued in production
until a trusted integration independently verifies these controls. Human approval
storage, authentication, audit logging, revocation, and replay protection are
also outside this module.

The helper resolves paths at validation time; a future executor must prevent
time-of-check/time-of-use symlink changes (for example by operating on a sealed
workspace or using directory-relative no-follow file operations). The current
policy also does not verify that a supplied workspace is the allowlisted remote
at the approved commit SHA, or that evidence excerpts match the approved source;
a trusted checkout/evidence layer must enforce those bindings.
The current policy is not an operating-system sandbox and must not be described
as one.
