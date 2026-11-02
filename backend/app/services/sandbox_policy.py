"""Fail-closed validation primitives for future repository test plans.

This module validates plans as data. It never executes commands or verifies an
executor's isolation; callers must supply trusted executor-control attestation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_ITEM_CHARS = 2_000
MAX_EVIDENCE_TOTAL_CHARS = 20_000
MAX_EVIDENCE_LINES = 200
MAX_EVIDENCE_PATH_BYTES = 1_024
MAX_EVIDENCE_TOTAL_BYTES = 32_000
MAX_SOURCE_EVIDENCE_READ_BYTES = 4_000_000
POLICY_VERSION = 2

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_TEMPLATE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_FORBIDDEN_TEMPLATE_TOKENS = {
    "sh",
    "bash",
    "zsh",
    "cmd",
    "powershell",
    "pwsh",
    "wscript",
    "cscript",
}


class SandboxPolicyError(ValueError):
    """A plan failed a sandbox policy check."""


def _canonical_repository(value: object) -> str:
    if not isinstance(value, str) or not _REPOSITORY_RE.fullmatch(value):
        raise SandboxPolicyError("repository must be an exact owner/repository slug")
    owner, repository = value.split("/", 1)
    if owner in {".", ".."} or repository in {".", ".."}:
        raise SandboxPolicyError("repository slug is invalid")
    return f"{owner.casefold()}/{repository.casefold()}"


def _canonical_sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise SandboxPolicyError("head SHA must be a full 40 or 64 character hex digest")
    return value.lower()


def validate_relative_repo_path(value: object) -> str:
    """Return a canonical repository-relative POSIX path or reject ambiguity."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise SandboxPolicyError("path must be a non-empty POSIX repository-relative path")
    if value.startswith("/") or ":" in value:
        raise SandboxPolicyError("absolute, drive, and alternate-stream paths are forbidden")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SandboxPolicyError("path traversal and empty path components are forbidden")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_PATH_BYTES or any(
        len(part.encode("utf-8")) > 255 for part in parts
    ):
        raise SandboxPolicyError("repository-relative path exceeds the size limit")
    return "/".join(parts)


def resolve_repo_path(repository_root: str | Path, relative_path: str) -> Path:
    """Resolve an existing path and reject traversal or symlink escape from the repo root."""
    canonical = validate_relative_repo_path(relative_path)
    try:
        root = Path(repository_root).resolve(strict=True)
        if not root.is_dir():
            raise SandboxPolicyError("repository root must be an existing directory")
        target = root.joinpath(*canonical.split("/")).resolve(strict=True)
        target.relative_to(root)
    except SandboxPolicyError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxPolicyError("path is missing, invalid, or outside the repository") from exc
    return target


@dataclass(frozen=True)
class Evidence:
    path: str
    start_line: int
    end_line: int
    excerpt: str

    @classmethod
    def from_data(cls, value: object) -> Evidence:
        if not isinstance(value, Mapping) or set(value) != {
            "path",
            "start_line",
            "end_line",
            "excerpt",
        }:
            raise SandboxPolicyError("evidence must contain exactly path, lines, and excerpt")
        path = validate_relative_repo_path(value["path"])
        start, end, excerpt = value["start_line"], value["end_line"], value["excerpt"]
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise SandboxPolicyError("evidence line range is invalid")
        if end - start + 1 > MAX_EVIDENCE_LINES:
            raise SandboxPolicyError("evidence line range exceeds the limit")
        if not isinstance(excerpt, str) or len(excerpt) > MAX_EVIDENCE_ITEM_CHARS:
            raise SandboxPolicyError("evidence excerpt exceeds the per-item limit")
        if len(excerpt.encode("utf-8")) > MAX_EVIDENCE_ITEM_CHARS:
            raise SandboxPolicyError("evidence excerpt exceeds the per-item byte limit")
        if len(excerpt.splitlines()) != end - start + 1:
            raise SandboxPolicyError("evidence excerpt line count does not match its range")
        return cls(path, start, end, excerpt)


@dataclass(frozen=True)
class SandboxPlan:
    """Strict data-only representation of an LLM-proposed bounded test plan."""

    repository: str
    head_sha: str
    test_template: str
    evidence: tuple[Evidence, ...]

    @classmethod
    def from_untrusted(cls, value: object) -> SandboxPlan:
        if not isinstance(value, Mapping) or set(value) != {
            "repository",
            "head_sha",
            "test_template",
            "evidence",
        }:
            raise SandboxPolicyError("plan has unknown or missing fields")
        repository = _canonical_repository(value["repository"])
        head_sha = _canonical_sha(value["head_sha"])
        template = value["test_template"]
        evidence_data = value["evidence"]
        if not isinstance(template, str) or not template:
            raise SandboxPolicyError("plan test template is required")
        if not isinstance(evidence_data, list) or len(evidence_data) > MAX_EVIDENCE_ITEMS:
            raise SandboxPolicyError("plan evidence count exceeds the limit")
        evidence = tuple(Evidence.from_data(item) for item in evidence_data)
        if _evidence_total_bytes(evidence) > MAX_EVIDENCE_TOTAL_BYTES:
            raise SandboxPolicyError("plan evidence exceeds the total byte limit")
        return cls(repository, head_sha, template, evidence)

    def digest(self) -> str:
        payload = {
            "repository": self.repository,
            "head_sha": self.head_sha,
            "test_template": self.test_template,
            "evidence": [
                {
                    "path": item.path,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "excerpt": item.excerpt,
                }
                for item in self.evidence
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RunnerTemplate:
    name: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TEMPLATE_NAME_RE.fullmatch(self.name):
            raise SandboxPolicyError("test template name must be a simple lowercase identifier")
        argv = tuple(self.argv)
        object.__setattr__(self, "argv", argv)
        if not argv:
            raise SandboxPolicyError("test template name and argv are required")
        if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
            raise SandboxPolicyError("test template argv must contain non-empty plain strings")
        executable = re.split(r"[/\\]", argv[0])[-1].casefold().removesuffix(".exe")
        if executable in _FORBIDDEN_TEMPLATE_TOKENS or any(
            arg.casefold()
            in {"-c", "/c", "-command", "-encodedcommand", "-e", "--eval", "--eval-file"}
            for arg in argv[1:]
        ):
            raise SandboxPolicyError("shell templates are forbidden")


DEFAULT_TEST_TEMPLATES = (
    RunnerTemplate("pytest", ("python", "-m", "pytest")),
    RunnerTemplate("ruff", ("ruff", "check", ".")),
    RunnerTemplate("npm-test", ("npm", "test", "--", "--run")),
)


@dataclass(frozen=True)
class SandboxPolicy:
    allowed_repositories: frozenset[str]
    test_templates: tuple[RunnerTemplate, ...] = DEFAULT_TEST_TEMPLATES
    max_evidence_items: int = MAX_EVIDENCE_ITEMS
    max_evidence_item_chars: int = MAX_EVIDENCE_ITEM_CHARS
    max_evidence_total_chars: int = MAX_EVIDENCE_TOTAL_CHARS
    max_evidence_lines: int = MAX_EVIDENCE_LINES

    def __post_init__(self) -> None:
        normalized = frozenset(_canonical_repository(repo) for repo in self.allowed_repositories)
        if not normalized:
            raise SandboxPolicyError("at least one repository must be explicitly allowlisted")
        object.__setattr__(self, "allowed_repositories", normalized)
        templates = tuple(self.test_templates)
        if any(not isinstance(template, RunnerTemplate) for template in templates):
            raise SandboxPolicyError("test templates must be trusted RunnerTemplate values")
        object.__setattr__(self, "test_templates", templates)
        names = [template.name for template in self.test_templates]
        if len(names) != len(set(names)):
            raise SandboxPolicyError("test template names must be unique")
        limits = (
            self.max_evidence_items,
            self.max_evidence_item_chars,
            self.max_evidence_total_chars,
            self.max_evidence_lines,
        )
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise SandboxPolicyError("evidence limits must be positive")
        hard_limits = (
            MAX_EVIDENCE_ITEMS,
            MAX_EVIDENCE_ITEM_CHARS,
            MAX_EVIDENCE_TOTAL_CHARS,
            MAX_EVIDENCE_LINES,
        )
        if any(limit > maximum for limit, maximum in zip(limits, hard_limits, strict=True)):
            raise SandboxPolicyError("configured evidence limits exceed hard safety caps")

    @property
    def policy_hash(self) -> str:
        payload = {
            "version": POLICY_VERSION,
            "repositories": sorted(self.allowed_repositories),
            "templates": [
                {"name": item.name, "argv": list(item.argv)}
                for item in sorted(self.test_templates, key=lambda item: item.name)
            ],
            "limits": {
                "evidence_items": self.max_evidence_items,
                "item_chars": self.max_evidence_item_chars,
                "total_chars": self.max_evidence_total_chars,
                "lines": self.max_evidence_lines,
                "path_bytes": MAX_EVIDENCE_PATH_BYTES,
                "total_bytes": MAX_EVIDENCE_TOTAL_BYTES,
                "source_read_bytes": MAX_SOURCE_EVIDENCE_READ_BYTES,
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def authorize(
        self,
        plan: SandboxPlan,
        approval: PlanApproval | None,
        executor_controls: ExecutorControls | None = None,
        *,
        repository_root: str | Path | None = None,
        approval_verifier: ApprovalVerifier | None = None,
        checkout_verifier: CheckoutVerifier | None = None,
        controls_verifier: ControlsVerifier | None = None,
    ) -> AuthorizedPlan:
        if not isinstance(plan, SandboxPlan):
            raise SandboxPolicyError("plan must be validated as data before authorization")
        repository = _canonical_repository(plan.repository)
        head_sha = _canonical_sha(plan.head_sha)
        if repository not in self.allowed_repositories:
            raise SandboxPolicyError("repository is not allowlisted")
        template = next(
            (item for item in self.test_templates if item.name == plan.test_template), None
        )
        if template is None:
            raise SandboxPolicyError("test template is not allowlisted")
        _validate_plan_evidence(plan, self)
        if not isinstance(approval, PlanApproval) or approval.approved is not True:
            raise SandboxPolicyError("explicit approval is required")
        if approval_verifier is None or approval_verifier.verify(
            approval, plan, self.policy_hash
        ) is not True:
            raise SandboxPolicyError("trusted, matching, single-use approval is required")
        if not isinstance(executor_controls, ExecutorControls) or controls_verifier is None:
            raise SandboxPolicyError("verified isolated executor controls are required")
        if controls_verifier.verify(executor_controls) is not True:
            raise SandboxPolicyError("verified isolated executor controls are required")
        if repository_root is None or checkout_verifier is None:
            raise SandboxPolicyError("verified repository checkout is required")
        try:
            resolved_root = Path(repository_root).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SandboxPolicyError("verified repository checkout is required") from exc
        if not resolved_root.is_dir():
            raise SandboxPolicyError("verified repository checkout is required")
        checkout = checkout_verifier.attest_checkout(repository, head_sha, resolved_root)
        try:
            attested_root = (
                checkout.root.resolve(strict=True)
                if isinstance(checkout, CheckoutAttestation) and isinstance(checkout.root, Path)
                else None
            )
        except (OSError, RuntimeError, ValueError):
            attested_root = None
        if not isinstance(checkout, CheckoutAttestation) or not (
            checkout.repository == repository
            and checkout.sha == head_sha
            and attested_root == resolved_root
            and checkout.immutable is True
            and isinstance(checkout.snapshot_id, str)
            and 1 <= len(checkout.snapshot_id.encode("utf-8")) <= 256
            and isinstance(checkout.tree_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", checkout.tree_digest)
        ):
            raise SandboxPolicyError(
                "checkout does not match the approved immutable repository and SHA"
            )
        source_read_budget = MAX_SOURCE_EVIDENCE_READ_BYTES
        for evidence in plan.evidence:
            evidence_path = resolve_repo_path(resolved_root, evidence.path)
            if not evidence_path.is_file():
                raise SandboxPolicyError("evidence path must refer to an existing file")
            actual, bytes_read = _read_evidence_lines(
                evidence_path,
                evidence.start_line,
                evidence.end_line,
                max_bytes=source_read_budget,
            )
            source_read_budget -= bytes_read
            if actual != evidence.excerpt:
                raise SandboxPolicyError(
                    "evidence excerpt does not match the approved source lines"
                )
        if approval_verifier.consume(approval, plan, self.policy_hash) is not True:
            raise SandboxPolicyError("trusted, matching, single-use approval is required")
        return AuthorizedPlan(
            repository,
            head_sha,
            template.name,
            template.argv,
            plan.digest(),
            str(resolved_root),
            checkout,
        )


def _read_evidence_lines(
    path: Path, start_line: int, end_line: int, *, max_bytes: int
) -> tuple[str, int]:
    selected: list[str] = []
    total_bytes = 0
    try:
        with path.open("rb") as source:
            for line_number in range(1, end_line + 1):
                remaining = max_bytes - total_bytes
                if remaining <= 0:
                    raise SandboxPolicyError("evidence source read exceeds the byte limit")
                raw = source.readline(remaining + 1)
                if len(raw) > remaining:
                    raise SandboxPolicyError("evidence source read exceeds the byte limit")
                if not raw:
                    raise SandboxPolicyError("evidence line range exceeds the source file")
                total_bytes += len(raw)
                if line_number >= start_line:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeError as exc:
                        raise SandboxPolicyError(
                            "evidence source must be readable UTF-8 text"
                        ) from exc
                    selected.append(text.removesuffix("\n").removesuffix("\r"))
    except OSError as exc:
        raise SandboxPolicyError("evidence source must be readable UTF-8 text") from exc
    return "\n".join(selected), total_bytes


def _validate_plan_evidence(plan: SandboxPlan, policy: SandboxPolicy) -> None:
    if not isinstance(plan.evidence, tuple):
        raise SandboxPolicyError("plan evidence must be immutable")
    if len(plan.evidence) > policy.max_evidence_items:
        raise SandboxPolicyError("plan evidence count exceeds policy limit")
    total = 0
    for evidence in plan.evidence:
        if not isinstance(evidence, Evidence):
            raise SandboxPolicyError("plan evidence has an invalid shape")
        validate_relative_repo_path(evidence.path)
        if (
            type(evidence.start_line) is not int
            or type(evidence.end_line) is not int
            or evidence.start_line < 1
            or evidence.end_line < evidence.start_line
        ):
            raise SandboxPolicyError("evidence line range is invalid")
        if not isinstance(evidence.excerpt, str):
            raise SandboxPolicyError("evidence excerpt must be text")
        if len(evidence.excerpt) > min(policy.max_evidence_item_chars, MAX_EVIDENCE_ITEM_CHARS):
            raise SandboxPolicyError("evidence excerpt exceeds policy item limit")
        if evidence.end_line - evidence.start_line + 1 > min(
            policy.max_evidence_lines, MAX_EVIDENCE_LINES
        ):
            raise SandboxPolicyError("evidence line range exceeds policy limit")
        if len(evidence.excerpt.splitlines()) != evidence.end_line - evidence.start_line + 1:
            raise SandboxPolicyError("evidence excerpt line count does not match its range")
        total += len(evidence.excerpt.encode("utf-8")) + len(evidence.path.encode("utf-8"))
    if total > MAX_EVIDENCE_TOTAL_BYTES:
        raise SandboxPolicyError("plan evidence exceeds the total byte limit")
    if sum(len(item.excerpt) for item in plan.evidence) > min(
        policy.max_evidence_total_chars, MAX_EVIDENCE_TOTAL_CHARS
    ):
        raise SandboxPolicyError("plan evidence exceeds policy total limit")


def _evidence_total_bytes(evidence: tuple[Evidence, ...]) -> int:
    return sum(
        len(item.excerpt.encode("utf-8")) + len(item.path.encode("utf-8")) for item in evidence
    )


@dataclass(frozen=True)
class PlanApproval:
    approved: bool
    plan_hash: str
    repository: str
    head_sha: str
    policy_hash: str


@dataclass(frozen=True)
class CheckoutAttestation:
    """Trusted proof identifying the immutable tree an executor must consume."""

    repository: str
    sha: str
    root: Path
    tree_digest: str
    snapshot_id: str
    immutable: bool


class ApprovalVerifier(Protocol):
    """Trusted service that authenticates approvals and atomically prevents replay."""

    def verify(
        self, approval: PlanApproval, plan: SandboxPlan, policy_hash: str
    ) -> bool: ...

    def consume(
        self, approval: PlanApproval, plan: SandboxPlan, policy_hash: str
    ) -> bool: ...


class CheckoutVerifier(Protocol):
    """Trusted service that returns a sealed snapshot identity for an approved checkout."""

    def attest_checkout(
        self, repository: str, sha: str, root: Path
    ) -> CheckoutAttestation | None: ...


class ControlsVerifier(Protocol):
    """Trusted service that measures/attests controls instead of trusting caller fields."""

    def verify(self, controls: ExecutorControls) -> bool: ...


@dataclass(frozen=True)
class ExecutorControls:
    """External attestations; values are not verified by this module."""

    isolated_container: bool = False
    ephemeral_workspace: bool = False
    network_disabled: bool = False
    secrets_unavailable: bool = False
    resource_limits_enforced: bool = False
    source_read_only: bool = False
    shell_disabled: bool = False

    @property
    def verified(self) -> bool:
        return all(
            value is True
            for value in (
                self.isolated_container,
                self.ephemeral_workspace,
                self.network_disabled,
                self.secrets_unavailable,
                self.resource_limits_enforced,
                self.source_read_only,
                self.shell_disabled,
            )
        )


@dataclass(frozen=True)
class AuthorizedPlan:
    repository: str
    head_sha: str
    test_template: str
    argv: tuple[str, ...]
    plan_hash: str
    repository_root: str | None = None
    checkout: CheckoutAttestation | None = None
