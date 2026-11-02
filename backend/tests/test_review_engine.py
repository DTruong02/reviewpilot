import asyncio
import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.schemas.review import (
    LLMFindings,
    ProviderOutput,
    PullRequestFile,
    RetrievedCodeContext,
    ReviewContext,
)
from app.services.context_builder import ContextBuilder, added_line_numbers
from app.services.context_retriever import ContextRetrievalResult
from app.services.github import (
    GitHubAppTokenProvider,
    GitHubClient,
    GitHubClientError,
    _request_with_retries,
    is_supported_source_path,
)
from app.services.llm import FakeLLMClient, LLMClientError, OpenAIReviewClient
from app.services.review_engine import ReviewEngine, ReviewEngineError

SHA = "a" * 40


def file_diff(path: str = "src/example.py") -> PullRequestFile:
    return PullRequestFile(
        path=path,
        status="modified",
        additions=1,
        deletions=1,
        patch="@@ -1,2 +1,2 @@\n keep\n-old()\n+new()\n",
    )


def review_context() -> ReviewContext:
    return ReviewContext(
        repository="octo/demo",
        pull_request_number=3,
        pull_request_title="change",
        head_sha=SHA,
        files=(
            {
                "file_path": "src/example.py",
                "patch": "@@ -1,2 +1,2 @@\n keep\n-old()\n+new()\n",
                "context": "1: keep\n2: new()",
                "added_lines": {2},
                "additions": 1,
                "deletions": 1,
            },
        ),
    )


def test_added_line_parser_uses_new_side_line_numbers():
    patch = "@@ -8,4 +10,5 @@\n before\n-old\n+replacement\n+extra\n after\n"
    assert added_line_numbers(patch) == {11, 12}


def test_github_path_filter_rejects_traversal_generated_and_binary_paths():
    assert not is_supported_source_path("../../secret.py")
    assert not is_supported_source_path("src/app.generated.py")
    assert not is_supported_source_path("assets/logo.png")
    assert is_supported_source_path("src/app.py")


@pytest.mark.asyncio
async def test_context_builder_caps_context_and_redacts_credentials():
    builder = ContextBuilder(max_files=5, max_changed_lines=10, max_context_chars=1000)
    context = await builder.build(
        repository="octo/demo",
        pull_request_number=3,
        pull_request_title="normal",
        head_sha=SHA,
        changed_files=[file_diff()],
        fetch_content=lambda _path: asyncio.sleep(
            0,
            result="\n".join(
                f"token = 'ghp_{'a' * 30}'" if index == 2 else f"line_{index}"
                for index in range(1, 60)
            ),
        ),
    )
    assert len(context.files) == 1
    assert sum(len(item.context) + len(item.patch) for item in context.files) <= 1000
    assert "ghp_" not in context.files[0].context
    assert context.files[0].added_lines == {2}
    assert "line_12" in context.files[0].context


@pytest.mark.asyncio
async def test_context_builder_honors_file_and_changed_line_budgets():
    builder = ContextBuilder(max_files=1, max_changed_lines=2, max_context_chars=2000)
    context = await builder.build(
        repository="octo/demo",
        pull_request_number=3,
        pull_request_title="normal",
        head_sha=SHA,
        changed_files=[file_diff("src/first.py"), file_diff("src/second.py")],
        fetch_content=lambda _path: asyncio.sleep(0, result="keep\nnew()\n"),
    )
    assert len(context.files) == 1
    assert context.files[0].file_path == "src/first.py"


@pytest.mark.asyncio
async def test_context_builder_does_not_fetch_after_budget_is_exhausted():
    calls = []

    async def fetch(_path):
        calls.append(1)
        return "keep\nnew()\n"

    context = await ContextBuilder(max_context_chars=100).build(
        repository="octo/demo",
        pull_request_number=3,
        pull_request_title="a title",
        head_sha=SHA,
        changed_files=[file_diff()],
        fetch_content=fetch,
    )
    assert context.files == ()
    assert calls == []


@pytest.mark.asyncio
async def test_token_provider_caches_token_until_safe_expiry_margin(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test.pem"
    key_path.write_bytes(private_bytes)
    now = [time.time()]
    count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        count[0] += 1
        assert request.url.path == "/app/installations/9/access_tokens"
        payload = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert payload["iss"] == "123"
        return httpx.Response(
            201,
            json={
                "token": f"secret-{count[0]}",
                "expires_at": (
                    datetime.fromtimestamp(now[0], tz=UTC) + timedelta(hours=1)
                ).isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = GitHubAppTokenProvider(
            123,
            key_path,
            http,
            api_url="https://api.github.com",
            cache_skew_seconds=60,
            now=lambda: now[0],
        )
        first = await provider.get_token(9)
        assert await provider.get_token(9) == first
        assert count[0] == 1
        now[0] = time.time() + 3600 * 24
        assert await provider.get_token(9) == "secret-2"
        assert count[0] == 2


@pytest.mark.asyncio
async def test_github_client_fetches_and_validates_current_pull_request_head():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/octo/demo/pulls/3")
        return httpx.Response(200, json={"head": {"sha": "A" * 40}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        github = GitHubClient(StaticToken(), http)
        assert await github.fetch_pull_request_head_sha(9, "octo", "demo", 3) == "a" * 40


@pytest.mark.asyncio
async def test_github_file_listing_paginates_and_applies_file_cap():
    calls: list[int] = []
    first_page = [
        {
            "filename": ("src/file-0.py" if index == 0 else f"vendor/file-{index}.py"),
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "@@ -1,0 +1,1 @@\n+pass",
        }
        for index in range(2)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200,
            json=first_page
            if page == 1
            else [
                {
                    "filename": "src/next.py",
                    "status": "added",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -0,0 +1,1 @@\n+pass",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        github = GitHubClient(
            token_provider=StaticToken(),
            http_client=http,
            api_url="https://api.github.com",
            max_files=2,
            max_changed_lines=1000,
        )
        files = await github.fetch_pull_request_files(9, "octo", "demo", 3)
        assert len(files) == 2
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_fetch_content_rejects_binary_and_oversized_files():
    encoded = base64.b64encode(b"safe text").decode()
    responses = [
        {"encoding": "base64", "size": 9, "content": encoded},
        {"encoding": "base64", "size": 999, "content": encoded},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        github = GitHubClient(
            token_provider=StaticToken(),
            http_client=http,
            max_source_file_bytes=10,
        )
        assert await github.fetch_file_content(9, "o", "r", "ok.py", SHA) == "safe text"
        assert await github.fetch_file_content(9, "o", "r", "large.py", SHA) is None


@pytest.mark.asyncio
async def test_github_client_retries_secondary_rate_limit_with_retry_after():
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(403, headers={"Retry-After": "0"}, json={"message": "limited"})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        github = GitHubClient(StaticToken(), http, max_files=2)
        assert await github.fetch_pull_request_files(9, "octo", "demo", 3) == []
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_github_response_body_is_capped_while_streaming():
    class LargeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"1234567"
            yield b"8901234"

        async def aclose(self):
            return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=LargeBody(), headers={})
        )
    ) as http:
        with pytest.raises(GitHubClientError, match="github_response_too_large"):
            await _request_with_retries(
                http,
                "GET",
                "https://api.github.com/test",
                headers={},
                max_response_bytes=10,
            )


@pytest.mark.asyncio
async def test_missing_file_context_is_skipped():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    ) as http:
        github = GitHubClient(StaticToken(), http)
        assert await github.fetch_file_content(9, "octo", "demo", "src/gone.py", SHA) is None


class StaticToken:
    async def get_token(self, installation_id: int) -> str:
        return "test-token"


class StaticGitHub:
    head_sha = SHA
    head_sha_sequence = None
    head_sha_calls = 0
    files_calls = 0

    async def fetch_pull_request_head_sha(self, installation_id, owner, repository, number):
        self.head_sha_calls += 1
        if self.head_sha_sequence:
            return self.head_sha_sequence.pop(0)
        return self.head_sha

    async def fetch_pull_request_files(self, installation_id, owner, repository, number):
        self.files_calls += 1
        return [file_diff()]

    async def fetch_file_content(self, installation_id, owner, repository, path, ref):
        return "keep\nnew()\n"


class SequenceLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.contexts = []

    async def review(self, context):
        self.calls += 1
        self.contexts.append(context)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return ProviderOutput(output=output, model_name="fake", duration_ms=2)


@pytest.mark.asyncio
async def test_review_engine_anchors_findings_and_discards_unanchored_and_duplicates():
    findings = LLMFindings(
        findings=[
            {
                "file_path": "src/example.py",
                "line_number": 2,
                "severity": "high",
                "category": "bug",
                "title": "Wrong result",
                "explanation": "The result is wrong.",
            },
            {
                "file_path": "src/example.py",
                "line_number": 1,
                "severity": "high",
                "category": "bug",
                "title": "Outside diff",
                "explanation": "Not added.",
            },
            {
                "file_path": "other.py",
                "line_number": 2,
                "severity": "low",
                "category": "bug",
                "title": "Invented path",
                "explanation": "Unknown file.",
            },
            {
                "file_path": "src/example.py",
                "line_number": 2,
                "severity": "medium",
                "category": "bug",
                "title": "Wrong result!",
                "explanation": "The result is wrong",
            },
        ]
    )
    engine = ReviewEngine(StaticGitHub(), SequenceLLM([findings]))
    result = await engine.review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
    )
    assert len(result.findings) == 1
    assert result.findings[0].line_number == 2


@pytest.mark.asyncio
async def test_stale_review_commit_is_superseded_before_diff_or_model_call():
    github = StaticGitHub()
    github.head_sha = "b" * 40
    llm = SequenceLLM([LLMFindings(findings=[])])

    result = await ReviewEngine(github, llm).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
    )

    assert result.stale_commit is True
    assert result.context_fallback_reason == "pull_request_head_changed"
    assert github.files_calls == 0
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_review_rechecks_head_after_diff_before_model_call():
    github = StaticGitHub()
    github.head_sha_sequence = [SHA, "b" * 40]
    llm = SequenceLLM([LLMFindings(findings=[])])

    result = await ReviewEngine(github, llm).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
    )

    assert github.head_sha_calls == 2
    assert github.files_calls == 1
    assert llm.calls == 0
    assert result.stale_commit is True


@pytest.mark.asyncio
async def test_review_engine_includes_context_and_rejects_unverified_citations():
    symbol_id = "e55d16b1-6837-465b-a166-6a628ec4a465"
    item = RetrievedCodeContext(
        file_path="src/helper.py",
        symbol_id=symbol_id,
        start_line=4,
        end_line=8,
        commit_sha=SHA,
        retrieval_reason="imported_file",
        score=0.75,
        snippet="4: def helper():\\n5: return True",
    )

    class Retriever:
        async def retrieve(self, **_kwargs):
            return ContextRetrievalResult("context_aware", None, (item,))

    findings = LLMFindings(
        findings=[
            {
                "file_path": "src/example.py",
                "line_number": 2,
                "severity": "high",
                "category": "bug",
                "title": "Changed behavior is incorrect",
                "explanation": "The added call depends on this helper.",
                "evidence_refs": [
                    {
                        "symbol_id": symbol_id,
                        "file_path": "src/helper.py",
                        "start_line": 4,
                        "end_line": 8,
                    }
                ],
            },
            {
                "file_path": "src/example.py",
                "line_number": 2,
                "severity": "high",
                "category": "bug",
                "title": "Unsupported citation",
                "explanation": "This refers to fabricated context.",
                "evidence_refs": [
                    {
                        "symbol_id": "missing-symbol",
                        "file_path": "secret.py",
                        "start_line": 1,
                        "end_line": 1,
                    }
                ],
            },
        ]
    )
    llm = SequenceLLM([findings])
    result = await ReviewEngine(
        StaticGitHub(), llm, context_retriever=Retriever()
    ).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
        repository_id=uuid.uuid4(),
    )

    assert result.context_mode == "context_aware"
    assert len(result.context_metadata) == 1
    assert not hasattr(result.context_metadata[0], "snippet")
    assert llm.contexts[0].retrieved_context[0].symbol_id == symbol_id
    assert len(result.findings) == 1
    assert result.findings[0].evidence_refs[0].symbol_id == symbol_id


@pytest.mark.asyncio
async def test_external_context_requires_repository_specific_source_consent(monkeypatch):
    monkeypatch.setattr(settings, "review_dry_run", False)
    monkeypatch.setattr(settings, "review_allow_source_upload", True)
    monkeypatch.setattr(settings, "review_context_repository_allowlist", "octo/another")

    class Retriever:
        calls = 0

        async def retrieve(self, **_kwargs):
            self.calls += 1
            return ContextRetrievalResult("context_aware", None, ())

    retriever = Retriever()
    result = await ReviewEngine(
        StaticGitHub(), SequenceLLM([LLMFindings(findings=[])]), context_retriever=retriever
    ).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
        repository_id=uuid.uuid4(),
    )

    assert retriever.calls == 0
    assert result.context_mode == "diff_only_fallback"
    assert result.context_fallback_reason == "context_source_upload_not_allowed"


@pytest.mark.asyncio
async def test_fixture_review_with_fake_provider_returns_a_validated_finding():
    fake = FakeLLMClient(
        LLMFindings(
            findings=[
                {
                    "file_path": "src/example.py",
                    "line_number": 2,
                    "severity": "medium",
                    "category": "bug",
                    "title": "Changed behavior loses result",
                    "explanation": "The replacement returns no value on the added line.",
                    "suggested_fix": "Return the computed value.",
                }
            ]
        )
    )
    result = await ReviewEngine(StaticGitHub(), fake).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
    )
    assert fake.calls == 1
    assert len(result.findings) == 1
    assert result.findings[0].line_number == 2


@pytest.mark.asyncio
async def test_invalid_model_output_retries_with_a_bounded_attempt_count():
    client = SequenceLLM([{"findings": [{"title": "missing required fields"}]}, {"findings": []}])
    result = await ReviewEngine(StaticGitHub(), client, validation_attempts=2).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
    )
    assert client.calls == 2
    assert result.findings == ()


@pytest.mark.asyncio
async def test_openai_invalid_structured_output_error_retries_once():
    client = SequenceLLM([LLMClientError("openai_empty_structured_output"), {"findings": []}])
    result = await ReviewEngine(StaticGitHub(), client, validation_attempts=2).review_pull_request(
        installation_id=1,
        owner="octo",
        repository="demo",
        pull_request_number=3,
        pull_request_title="title",
        head_sha=SHA,
    )
    assert client.calls == 2
    assert result.findings == ()


@pytest.mark.asyncio
async def test_permanent_llm_error_fails_without_retry():
    client = SequenceLLM([LLMClientError("openai_not_configured"), {"findings": []}])
    with pytest.raises(ReviewEngineError, match="openai_not_configured"):
        await ReviewEngine(StaticGitHub(), client).review_pull_request(
            installation_id=1,
            owner="octo",
            repository="demo",
            pull_request_number=3,
            pull_request_title="title",
            head_sha=SHA,
        )
    assert client.calls == 1


@pytest.mark.asyncio
async def test_invalid_model_output_fails_after_retry_limit():
    client = SequenceLLM([{"findings": [{"title": "bad"}]}, {"findings": [{"title": "bad"}]}])
    with pytest.raises(ReviewEngineError, match="llm_invalid_output"):
        await ReviewEngine(StaticGitHub(), client).review_pull_request(
            installation_id=1,
            owner="octo",
            repository="demo",
            pull_request_number=3,
            pull_request_title="title",
            head_sha=SHA,
        )


@pytest.mark.asyncio
async def test_fake_client_supports_empty_findings_and_openai_requires_opt_in():
    assert (await FakeLLMClient().review(review_context())).output.findings == []
    client = OpenAIReviewClient(api_key="secret", model="configurable")
    with pytest.raises(LLMClientError, match="source_upload_not_opted_in"):
        await client.review(review_context())


@pytest.mark.asyncio
async def test_openai_client_uses_pydantic_structured_output_parse():
    calls = []

    class Responses:
        async def parse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                output_parsed=LLMFindings(findings=[]),
                usage=SimpleNamespace(input_tokens=12, output_tokens=4),
            )

    class SDK:
        responses = Responses()

        async def close(self):
            return None

    provider = OpenAIReviewClient(
        api_key="test-key",
        model="model-from-config",
        allow_source_upload=True,
        client=SDK(),
    )
    output = await provider.review(review_context())
    assert calls[0]["model"] == "model-from-config"
    assert calls[0]["text_format"] is LLMFindings
    assert output.input_tokens == 12
    assert output.output_tokens == 4
    assert output.output.findings == []
