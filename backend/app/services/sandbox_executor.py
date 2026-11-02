"""Fail-closed execution boundary for finding validation.

The application has no verified isolated executor. The default adapter must
therefore block every approved request without starting a process or job.
"""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class SandboxRequest:
    repository: str
    commit_sha: str
    plan_hash: str


@dataclass(frozen=True)
class SandboxResult:
    status: Literal["blocked"]
    conclusion: Literal["inconclusive"]
    executor: str
    observation: str


class SandboxExecutor(Protocol):
    async def run(self, request: SandboxRequest) -> SandboxResult: ...


class UnavailableSandboxExecutor:
    """Default adapter: explicitly block because isolation is not verified."""

    async def run(self, request: SandboxRequest) -> SandboxResult:
        del request  # The disabled adapter does not inspect or fetch repository data.
        return SandboxResult(
            status="blocked",
            conclusion="inconclusive",
            executor="unavailable",
            observation=(
                "Validation was blocked because sandbox isolation is unverified. "
                "No code was run and no job was queued."
            ),
        )
