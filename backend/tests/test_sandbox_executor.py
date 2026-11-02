import pytest

from app.services.sandbox_executor import SandboxRequest, UnavailableSandboxExecutor


@pytest.mark.asyncio
async def test_unavailable_executor_fails_closed_without_claiming_execution():
    result = await UnavailableSandboxExecutor().run(
        SandboxRequest(
            repository="octo/demo",
            commit_sha="a" * 40,
            plan_hash="b" * 64,
        )
    )

    assert result.status == "blocked"
    assert result.conclusion == "inconclusive"
    assert result.executor == "unavailable"
    assert "isolation is unverified" in result.observation
    assert "No code was run" in result.observation
    assert "no job was queued" in result.observation
