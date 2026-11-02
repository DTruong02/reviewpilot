from __future__ import annotations

from pathlib import Path

import pytest

import app.services.sandbox_policy as sandbox_policy
from app.services.sandbox_policy import (
    DEFAULT_TEST_TEMPLATES,
    MAX_EVIDENCE_ITEM_CHARS,
    CheckoutAttestation,
    ExecutorControls,
    PlanApproval,
    RunnerTemplate,
    SandboxPlan,
    SandboxPolicy,
    SandboxPolicyError,
    resolve_repo_path,
)

SHA = "a" * 40


class MockApprovalVerifier:
    def __init__(self) -> None:
        self.consumed: set[str] = set()

    def verify(self, approval, plan, policy_hash) -> bool:
        if (
            approval.approved is not True
            or approval.plan_hash != plan.digest()
            or approval.repository.casefold() != plan.repository.casefold()
            or approval.head_sha.lower() != plan.head_sha.lower()
            or approval.policy_hash != policy_hash
            or approval.plan_hash in self.consumed
        ):
            return False
        return True

    def consume(self, approval, plan, policy_hash) -> bool:
        if not self.verify(approval, plan, policy_hash):
            return False
        self.consumed.add(approval.plan_hash)
        return True


class MockCheckoutVerifier:
    def __init__(self, repository: str, sha: str, root: Path) -> None:
        self.repository, self.sha, self.root = repository, sha, root.resolve()

    def attest_checkout(self, repository: str, sha: str, root: Path):
        if (repository, sha, root.resolve()) != (self.repository, self.sha, self.root):
            return None
        return CheckoutAttestation(repository, sha, root, "b" * 64, "sealed-test", True)


class MockControlsVerifier:
    def verify(self, controls) -> bool:
        return controls.verified


def make_policy() -> SandboxPolicy:
    return SandboxPolicy(frozenset({"Example/Allowed-Repo"}))


def make_plan(**changes) -> SandboxPlan:
    data = {
        "repository": "example/allowed-repo",
        "head_sha": SHA,
        "test_template": "pytest",
        "evidence": [],
    }
    data.update(changes)
    return SandboxPlan.from_untrusted(data)


def make_approval(policy: SandboxPolicy, plan: SandboxPlan, **changes) -> PlanApproval:
    data = {
        "approved": True,
        "plan_hash": plan.digest(),
        "repository": plan.repository,
        "head_sha": plan.head_sha,
        "policy_hash": policy.policy_hash,
    }
    data.update(changes)
    return PlanApproval(**data)


def authorize(policy, plan, approval, controls=None, *, root=None, checkout=None, verifier=None):
    checkout_root = root or Path(__file__).resolve().parent
    return policy.authorize(
        plan,
        approval,
        controls,
        repository_root=checkout_root,
        approval_verifier=verifier or MockApprovalVerifier(),
        checkout_verifier=checkout
        or MockCheckoutVerifier(plan.repository, plan.head_sha, checkout_root),
        controls_verifier=MockControlsVerifier(),
    )


def safe_controls(**changes) -> ExecutorControls:
    data = {
        "isolated_container": True,
        "ephemeral_workspace": True,
        "network_disabled": True,
        "secrets_unavailable": True,
        "resource_limits_enforced": True,
        "source_read_only": True,
        "shell_disabled": True,
    }
    data.update(changes)
    return ExecutorControls(**data)


def test_plan_is_data_only_and_authorization_returns_exact_allowlisted_argv() -> None:
    policy = make_policy()
    plan = make_plan()
    authorized = authorize(policy, plan, make_approval(policy, plan), safe_controls())

    assert authorized.repository == "example/allowed-repo"
    assert authorized.head_sha == SHA
    assert authorized.argv == ("python", "-m", "pytest")
    assert authorized.plan_hash == plan.digest()
    assert not hasattr(plan, "command")
    assert DEFAULT_TEST_TEMPLATES


@pytest.mark.parametrize(
    "data",
    [
        {
            "repository": "example/allowed-repo",
            "head_sha": SHA,
            "test_template": "pytest",
            "evidence": [],
            "command": "pytest",
        },
        {
            "repository": "example/allowed-repo",
            "head_sha": "short",
            "test_template": "pytest",
            "evidence": [],
        },
        {
            "repository": "../outside/repo",
            "head_sha": SHA,
            "test_template": "pytest",
            "evidence": [],
        },
        {
            "repository": "example/allowed-repo",
            "head_sha": SHA,
            "test_template": "pytest",
            "evidence": "not-an-array",
        },
    ],
)
def test_untrusted_plan_rejects_extra_fields_and_malformed_identity(data) -> None:
    with pytest.raises(SandboxPolicyError):
        SandboxPlan.from_untrusted(data)


def test_authorization_requires_exact_repo_template_explicit_matching_approval_and_controls() -> (
    None
):
    policy = make_policy()
    plan = make_plan()
    approval = make_approval(policy, plan)

    with pytest.raises(SandboxPolicyError, match="explicit approval"):
        authorize(policy, plan, None, safe_controls())
    with pytest.raises(SandboxPolicyError, match="explicit approval"):
        authorize(policy, plan, make_approval(policy, plan, approved=False), safe_controls())
    with pytest.raises(SandboxPolicyError, match="single-use approval"):
        authorize(policy, plan, make_approval(policy, plan, head_sha="b" * 40), safe_controls())
    with pytest.raises(SandboxPolicyError, match="single-use approval"):
        authorize(policy, plan, make_approval(policy, plan, policy_hash="0" * 64), safe_controls())
    with pytest.raises(SandboxPolicyError, match="verified isolated executor"):
        authorize(policy, plan, approval)
    with pytest.raises(SandboxPolicyError, match="verified isolated executor"):
        authorize(policy, plan, approval, safe_controls(network_disabled=False))

    disallowed = make_plan(repository="attacker/repo")
    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        authorize(policy, disallowed, make_approval(policy, disallowed), safe_controls())
    unknown_template = make_plan(test_template="pytest --dangerous")
    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        authorize(
            policy,
            unknown_template,
            make_approval(policy, unknown_template),
            safe_controls(),
        )


def test_policy_hash_binds_repositories_templates_and_limits(monkeypatch) -> None:
    original = make_policy()
    changed_repo = SandboxPolicy(frozenset({"example/other-repo"}))
    changed_template = SandboxPolicy(
        frozenset({"example/allowed-repo"}),
        test_templates=(RunnerTemplate("pytest", ("python", "-m", "pytest", "-q")),),
    )
    changed_limit = SandboxPolicy(frozenset({"example/allowed-repo"}), max_evidence_items=10)

    assert len(original.policy_hash) == 64
    assert (
        len(
            {
                original.policy_hash,
                changed_repo.policy_hash,
                changed_template.policy_hash,
                changed_limit.policy_hash,
            }
        )
        == 4
    )
    prior_hash = original.policy_hash
    monkeypatch.setattr(sandbox_policy, "MAX_SOURCE_EVIDENCE_READ_BYTES", 3_999_999)
    assert make_policy().policy_hash != prior_hash
    with pytest.raises(SandboxPolicyError, match="hard safety caps"):
        SandboxPolicy(frozenset({"example/allowed-repo"}), max_evidence_items=21)


@pytest.mark.parametrize(
    "argv",
    [
        ("bash", "-c", "echo unsafe"),
        ("cmd.exe", "/c", "whoami"),
        ("pwsh", "-Command", "Get-ChildItem"),
        (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-File", "x.ps1"),
        ("python", "-c", "print('unsafe')"),
    ],
)
def test_policy_refuses_shell_test_templates(argv: tuple[str, ...]) -> None:
    with pytest.raises(SandboxPolicyError, match="shell templates"):
        RunnerTemplate("unsafe", argv)


def test_evidence_count_excerpt_and_line_limits_are_bounded() -> None:
    policy = make_policy()
    evidence = {
        "path": "src/module.py",
        "start_line": 1,
        "end_line": 2,
        "excerpt": "x" * (MAX_EVIDENCE_ITEM_CHARS + 1),
    }
    with pytest.raises(SandboxPolicyError, match="per-item limit"):
        make_plan(evidence=[evidence])
    with pytest.raises(SandboxPolicyError, match="line range"):
        make_plan(evidence=[{**evidence, "excerpt": "x", "end_line": 202}])
    too_many = [
        {"path": f"src/{i}.py", "start_line": 1, "end_line": 1, "excerpt": "x"} for i in range(21)
    ]
    with pytest.raises(SandboxPolicyError, match="count exceeds"):
        make_plan(evidence=too_many)
    assert policy.max_evidence_items < 21


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "src\\..\\outside.py",
        "src//file.py",
        "src/file.py:stream",
    ],
)
def test_relative_path_validator_rejects_traversal_and_ambiguous_paths(
    path: str, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(SandboxPolicyError):
        resolve_repo_path(tmp_path, path)


def test_repository_path_resolves_inside_repo_and_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (repo / "src").mkdir(parents=True)
    outside.mkdir()
    target = repo / "src" / "module.py"
    target.write_text("pass\n", encoding="utf-8")
    assert resolve_repo_path(repo, "src/module.py") == target.resolve()

    escape = repo / "src" / "external"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable in this environment")
    with pytest.raises(SandboxPolicyError, match="outside the repository"):
        resolve_repo_path(repo, "src/external")


def test_evidence_requires_contained_existing_path_before_authorization(tmp_path: Path) -> None:
    policy = make_policy()
    plan = make_plan(
        evidence=[
            {
                "path": "src/module.py",
                "start_line": 1,
                "end_line": 1,
                "excerpt": "pass",
            }
        ]
    )
    approval = make_approval(policy, plan)
    with pytest.raises(SandboxPolicyError, match="verified repository checkout is required"):
        policy.authorize(
            plan,
            approval,
            safe_controls(),
            approval_verifier=MockApprovalVerifier(),
            controls_verifier=MockControlsVerifier(),
        )
    with pytest.raises(SandboxPolicyError, match="missing, invalid, or outside"):
        authorize(
            policy,
            plan,
            approval,
            safe_controls(),
            root=tmp_path,
            checkout=MockCheckoutVerifier("example/allowed-repo", SHA, tmp_path),
        )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("pass\n", encoding="utf-8")
    assert (
        authorize(
            policy,
            plan,
            approval,
            safe_controls(),
            root=tmp_path,
            checkout=MockCheckoutVerifier("example/allowed-repo", SHA, tmp_path),
        ).test_template
        == "pytest"
    )


def test_approval_verifier_rejects_forged_and_replayed_approval() -> None:
    policy = make_policy()
    plan = make_plan()
    verifier = MockApprovalVerifier()
    approval = make_approval(policy, plan)
    authorize(policy, plan, approval, safe_controls(), verifier=verifier)

    with pytest.raises(SandboxPolicyError, match="single-use approval"):
        authorize(policy, plan, approval, safe_controls(), verifier=verifier)

    forged = make_approval(policy, plan, plan_hash="0" * 64)
    with pytest.raises(SandboxPolicyError, match="single-use approval"):
        authorize(policy, plan, forged, safe_controls(), verifier=verifier)


def test_failed_validation_does_not_consume_approval() -> None:
    policy = make_policy()
    plan = make_plan()
    approval = make_approval(policy, plan)
    verifier = MockApprovalVerifier()

    with pytest.raises(SandboxPolicyError, match="verified isolated executor controls"):
        authorize(
            policy,
            plan,
            approval,
            safe_controls(network_disabled=False),
            verifier=verifier,
        )

    authorize(policy, plan, approval, safe_controls(), verifier=verifier)


def test_checkout_and_evidence_are_bound_to_verified_source(tmp_path: Path) -> None:
    policy = make_policy()
    plan = make_plan(
        evidence=[{"path": "src/module.py", "start_line": 1, "end_line": 1, "excerpt": "pass"}]
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("raise SystemExit()\n", encoding="utf-8")
    approval = make_approval(policy, plan)

    wrong_checkout = MockCheckoutVerifier("attacker/repo", SHA, tmp_path)
    with pytest.raises(SandboxPolicyError, match="checkout does not match"):
        authorize(policy, plan, approval, safe_controls(), root=tmp_path, checkout=wrong_checkout)

    trusted_checkout = MockCheckoutVerifier("example/allowed-repo", SHA, tmp_path)
    with pytest.raises(SandboxPolicyError, match="does not match the approved source"):
        authorize(policy, plan, approval, safe_controls(), root=tmp_path, checkout=trusted_checkout)


def test_evidence_path_length_is_bounded() -> None:
    with pytest.raises(SandboxPolicyError, match="size limit"):
        make_plan(
            evidence=[
                {
                    "path": "src/" + "a" * 1_030,
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt": "x",
                }
            ]
        )


def test_evidence_reads_share_one_bounded_source_byte_budget(tmp_path: Path, monkeypatch) -> None:
    policy = make_policy()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x\nx\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("x\nx\n", encoding="utf-8")
    monkeypatch.setattr(sandbox_policy, "MAX_SOURCE_EVIDENCE_READ_BYTES", 6)
    plan = make_plan(
        evidence=[
            {"path": "src/a.py", "start_line": 2, "end_line": 2, "excerpt": "x"},
            {"path": "src/b.py", "start_line": 2, "end_line": 2, "excerpt": "x"},
        ]
    )

    with pytest.raises(SandboxPolicyError, match="source read exceeds the byte limit"):
        authorize(
            policy,
            plan,
            make_approval(policy, plan),
            safe_controls(),
            root=tmp_path,
            checkout=MockCheckoutVerifier("example/allowed-repo", SHA, tmp_path),
        )
