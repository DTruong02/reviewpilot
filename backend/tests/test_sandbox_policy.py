from __future__ import annotations

from pathlib import Path

import pytest

from app.services.sandbox_policy import (
    DEFAULT_TEST_TEMPLATES,
    MAX_EVIDENCE_ITEM_CHARS,
    ExecutorControls,
    PlanApproval,
    RunnerTemplate,
    SandboxPlan,
    SandboxPolicy,
    SandboxPolicyError,
    resolve_repo_path,
)

SHA = "a" * 40


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
    authorized = policy.authorize(plan, make_approval(policy, plan), safe_controls())

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
        policy.authorize(plan, None, safe_controls())
    with pytest.raises(SandboxPolicyError, match="explicit approval"):
        policy.authorize(plan, make_approval(policy, plan, approved=False), safe_controls())
    with pytest.raises(SandboxPolicyError, match="does not match"):
        policy.authorize(plan, make_approval(policy, plan, head_sha="b" * 40), safe_controls())
    with pytest.raises(SandboxPolicyError, match="does not match"):
        policy.authorize(plan, make_approval(policy, plan, policy_hash="0" * 64), safe_controls())
    with pytest.raises(SandboxPolicyError, match="verified isolated executor"):
        policy.authorize(plan, approval)
    with pytest.raises(SandboxPolicyError, match="verified isolated executor"):
        policy.authorize(plan, approval, safe_controls(network_disabled=False))

    disallowed = make_plan(repository="attacker/repo")
    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        policy.authorize(disallowed, make_approval(policy, disallowed), safe_controls())
    unknown_template = make_plan(test_template="pytest --dangerous")
    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        policy.authorize(unknown_template, make_approval(policy, unknown_template), safe_controls())


def test_policy_hash_binds_repositories_templates_and_limits() -> None:
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
    with pytest.raises(SandboxPolicyError, match="repository root is required"):
        policy.authorize(plan, approval, safe_controls())
    with pytest.raises(SandboxPolicyError, match="missing, invalid, or outside"):
        policy.authorize(plan, approval, safe_controls(), repository_root=tmp_path)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("pass\n", encoding="utf-8")
    assert (
        policy.authorize(plan, approval, safe_controls(), repository_root=tmp_path).test_template
        == "pytest"
    )
