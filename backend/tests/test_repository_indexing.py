import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.indexes import _embedding_profile
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.entities import (
    CodeFile,
    CodeSymbol,
    EmbeddingCache,
    Installation,
    PullRequest,
    Repository,
    RepositoryIndex,
    Review,
    ReviewStatus,
    SymbolEdge,
)
from app.services.embeddings import (
    DeterministicHashEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from app.services.github import GitHubClient, GitHubClientError, GitTreeFile
from app.services.repository_indexer import RepositoryIndexer
from app.workers import index_tasks

COMMIT = "a" * 40


class FakeGitHub:
    def __init__(self):
        self.sources = {
            "src/main.js": (
                'import { helper } from "./utils";\n'
                "export function run(input) { return helper(input); }\n"
            ),
            "src/utils.js": "export function helper(value) { return value; }\n",
            "src/secret.py": 'password = "supersecretcredentialvalue"\n',
        }
        self.tree = [
            GitTreeFile(path, _blob_sha(source.encode("utf-8")), len(source.encode("utf-8")))
            for path, source in self.sources.items()
        ]
        self.fetches: list[str] = []
        self.tree_calls = 0
        self.fail_tree = False

    async def fetch_commit_tree(self, installation_id, *_args, **_kwargs):
        assert installation_id == 700
        self.tree_calls += 1
        if self.fail_tree:
            raise GitHubClientError("github_rate_limited", 403)
        return list(self.tree)

    async def fetch_file_content(self, installation_id, _owner, _repo, path, ref):
        assert installation_id == 700
        assert ref in {COMMIT, "b" * 40}
        self.fetches.append(path)
        return self.sources.get(path)


def _blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


class CountingEmbeddings(DeterministicHashEmbeddingProvider):
    def __init__(self, dimension: int = 8):
        super().__init__(dimension)
        self.model_name = "mock-embed"
        self.model_version = "fixture-v1"
        self.calls: list[Sequence[str]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return await super().embed(texts)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        installation = Installation(github_installation_id=700, account_login="octo")
        session.add(installation)
        await session.flush()
        repository = Repository(
            installation_id=installation.id,
            github_repository_id=701,
            owner="octo",
            name="index-fixture",
            full_name="octo/index-fixture",
            private=True,
        )
        session.add(repository)
        await session.flush()
        index = RepositoryIndex(
            id=uuid.uuid4(),
            repository_id=repository.id,
            commit_sha=COMMIT,
            status="queued",
            parser_version="tree-sitter-symbols-v1",
            embedding_model="mock-embed",
            embedding_version="fixture-v1",
            embedding_dimension=8,
            embedding_enabled=True,
        )
        session.add(index)
    yield engine, sessions, repository.id, index.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_indexes_immutable_commit_resumes_and_caches_embeddings(db):
    _engine, sessions, repository_id, index_id = db
    github = FakeGitHub()
    embeddings = CountingEmbeddings()
    indexer = RepositoryIndexer(sessions, github, embeddings=embeddings)

    result = await indexer.index_commit(index_id)
    assert result.status == "ready"
    assert result.files_indexed == 2
    assert result.files_skipped == 1
    assert result.symbols_indexed == 2
    assert github.fetches == ["src/main.js", "src/utils.js", "src/secret.py"]
    assert github.tree_calls == 1
    assert len(embeddings.calls) == 2
    async with sessions() as session:
        paths = list(
            await session.scalars(select(CodeFile.path).where(CodeFile.index_id == index_id))
        )
        assert set(paths) == {item.path for item in github.tree}
        secret = await session.scalar(
            select(CodeFile).where(CodeFile.index_id == index_id, CodeFile.path == "src/secret.py")
        )
        assert secret.status == "skipped"
        assert secret.skip_reason == "secret_pattern_detected"
        edge_count = await session.scalar(
            select(func.count()).select_from(SymbolEdge).where(SymbolEdge.index_id == index_id)
        )
        assert edge_count == 1
        symbols = list(
            await session.scalars(
                select(CodeSymbol).join(CodeFile).where(CodeFile.index_id == index_id)
            )
        )
        assert all(len(symbol.embedding) == 8 for symbol in symbols)
        assert not any(hasattr(symbol, "signature") for symbol in symbols)

    replay = await indexer.index_commit(index_id)
    assert replay.status == "ready"
    assert replay.symbols_indexed == 2
    assert len(embeddings.calls) == 2

    # Same source under a distinct immutable SHA reuses vectors, but gets isolated files/symbols.
    other = RepositoryIndex(
        id=uuid.uuid4(),
        repository_id=repository_id,
        commit_sha="b" * 40,
        status="queued",
        parser_version="tree-sitter-symbols-v1",
        embedding_model="mock-embed",
        embedding_version="fixture-v1",
        embedding_dimension=8,
        embedding_enabled=True,
    )
    async with sessions.begin() as session:
        session.add(other)
    second = await indexer.index_commit(other.id)
    assert second.status == "ready"
    assert len(embeddings.calls) == 2
    async with sessions() as session:
        index_count = await session.scalar(
            select(func.count())
            .select_from(RepositoryIndex)
            .where(RepositoryIndex.repository_id == repository_id)
        )
        cache_count = await session.scalar(select(func.count()).select_from(EmbeddingCache))
        assert index_count == 2
        assert cache_count == 2


@pytest.mark.asyncio
async def test_index_failure_is_visible_and_retry_resumes(db):
    _engine, sessions, _repository_id, index_id = db
    github = FakeGitHub()
    github.fail_tree = True
    indexer = RepositoryIndexer(sessions, github)
    failed = await indexer.index_commit(index_id)
    assert failed.status == "failed"
    assert failed.error_code == "github_rate_limited"
    github.fail_tree = False
    async with sessions.begin() as session:
        index = await session.get(RepositoryIndex, index_id)
        index.status = "queued"
    ready = await indexer.index_commit(index_id)
    assert ready.status == "ready"
    assert ready.error_code is None


@pytest.mark.asyncio
async def test_file_limit_fails_without_fetching_partial_snapshot(db):
    _engine, sessions, _repository_id, index_id = db
    github = FakeGitHub()
    result = await RepositoryIndexer(sessions, github, max_files=1).index_commit(index_id)
    assert result.status == "failed"
    assert result.error_code == "index_file_limit_exceeded"
    assert github.fetches == []


@pytest.mark.asyncio
async def test_github_tree_filters_unsafe_paths_modes_and_symlinks():
    class Tokens:
        async def get_token(self, _installation_id):
            return "token"

    payload = {
        "truncated": False,
        "tree": [
            {"path": "src/good.py", "type": "blob", "mode": "100644", "size": 10, "sha": "a" * 40},
            {
                "path": "../../outside.py",
                "type": "blob",
                "mode": "100644",
                "size": 10,
                "sha": "b" * 40,
            },
            {"path": ".env", "type": "blob", "mode": "100644", "size": 10, "sha": "c" * 40},
            {"path": "link.py", "type": "blob", "mode": "120000", "size": 10, "sha": "d" * 40},
            {"path": "deps", "type": "commit", "mode": "160000", "size": 10, "sha": "e" * 40},
        ],
    }
    from httpx import AsyncClient, MockTransport, Response

    async def respond(_request):
        return Response(200, json=payload)

    http = AsyncClient(transport=MockTransport(respond))
    github = GitHubClient(Tokens(), http, api_url="https://github.test")
    files = await github.fetch_commit_tree(7, "octo", "repo", COMMIT)
    await http.aclose()
    assert [item.path for item in files] == ["src/good.py"]


@pytest.mark.asyncio
async def test_index_status_api_is_sha_scoped_and_submission_is_idempotent(db, monkeypatch):
    engine, sessions, repository_id, index_id = db
    dispatches = []
    monkeypatch.setattr(
        index_tasks.index_repository_task, "delay", lambda value: dispatches.append(value)
    )

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/repositories/{repository_id}/indexes/{COMMIT}")
            assert response.status_code == 200
            assert response.json()["id"] == str(index_id)
            assert response.json()["status"] == "queued"
            posted = await client.post(
                f"/api/repositories/{repository_id}/indexes", json={"commit_sha": COMMIT}
            )
            assert posted.status_code == 200
            assert posted.json()["id"] == str(index_id)
            assert dispatches == []
            new_commit = "c" * 40
            first = await client.post(
                f"/api/repositories/{repository_id}/indexes", json={"commit_sha": new_commit}
            )
            assert first.status_code == 202
            assert first.json()["embedding_model"] == "local-deterministic-mock"
            assert len(dispatches) == 1
            duplicate = await client.post(
                f"/api/repositories/{repository_id}/indexes", json={"commit_sha": new_commit}
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["id"] == first.json()["id"]
            assert len(dispatches) == 1
            status_response = await client.get(
                f"/api/repositories/{repository_id}/indexes/{new_commit}"
            )
            assert status_response.json()["commit_sha"] == new_commit
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_reconciler_requeues_stale_indexing_rows(db):
    _engine, sessions, _repository_id, index_id = db
    async with sessions.begin() as session:
        index = await session.get(RepositoryIndex, index_id)
        index.status = "indexing"
        index.started_at = datetime.now(UTC) - timedelta(hours=1)
        index.updated_at = datetime.now(UTC) - timedelta(hours=1)
    dispatched = []
    count = await index_tasks.run_index_reconciliation_async(
        sessions=sessions, enqueue=dispatched.append
    )
    assert count == 1
    assert dispatched == [str(index_id)]
    async with sessions() as session:
        index = await session.get(RepositoryIndex, index_id)
        assert index.status == "queued"


@pytest.mark.asyncio
async def test_openai_embedding_adapter_is_injectable_and_requests_configured_dimension():
    class SDKEmbeddings:
        def __init__(self):
            self.call = None

        async def create(self, **kwargs):
            self.call = kwargs
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.25, 0.75])])

    sdk = SDKEmbeddings()
    provider = OpenAIEmbeddingProvider(
        "mock-key", "embedding-fixture", 2, client=SimpleNamespace(embeddings=sdk)
    )
    vectors = await provider.embed(["short symbol summary"])
    assert vectors == [[0.25, 0.75]]
    assert sdk.call == {
        "model": "embedding-fixture",
        "input": ["short symbol summary"],
        "dimensions": 2,
    }


def test_external_embedding_requires_upload_consent_mode_and_repository_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "index_embedding_mode", "openai")
    monkeypatch.setattr(settings, "index_embedding_repository_allowlist", "octo/index-fixture")
    monkeypatch.setattr(settings, "review_allow_source_upload", False)
    assert _embedding_profile("octo/index-fixture") == (False, "disabled", "disabled")
    monkeypatch.setattr(settings, "review_allow_source_upload", True)
    assert _embedding_profile("elsewhere/repo") == (False, "disabled", "disabled")
    assert _embedding_profile("octo/index-fixture")[0] is True
    monkeypatch.setattr(settings, "index_embedding_mode", "mock")
    monkeypatch.setattr(settings, "review_allow_source_upload", False)
    assert _embedding_profile("any/repository") == (
        True,
        "local-deterministic-mock",
        "sha256-projection-v1",
    )


@pytest.mark.asyncio
async def test_index_retention_removes_only_expired_unreferenced_indexes(db):
    _engine, sessions, repository_id, referenced_id = db
    old = datetime.now(UTC) - timedelta(days=100)
    unreferenced_id = uuid.uuid4()
    file_id = uuid.uuid4()
    async with sessions.begin() as session:
        referenced = await session.get(RepositoryIndex, referenced_id)
        referenced.status = "ready"
        referenced.created_at = old
        pull_request = PullRequest(
            repository_id=repository_id,
            github_pr_number=99,
            title="Historical review",
            state="closed",
            head_sha=COMMIT,
            html_url="https://github.com/octo/index-fixture/pull/99",
        )
        session.add(pull_request)
        await session.flush()
        session.add(
            Review(
                pull_request_id=pull_request.id,
                commit_sha=COMMIT,
                status=ReviewStatus.completed,
            )
        )
        session.add(
            RepositoryIndex(
                id=unreferenced_id,
                repository_id=repository_id,
                commit_sha="b" * 40,
                status="ready",
                parser_version="tree-sitter-symbols-v1",
                embedding_model="mock-embed",
                embedding_version="fixture-v1",
                embedding_dimension=8,
                embedding_enabled=True,
                created_at=old,
            )
        )
        session.add(
            CodeFile(
                id=file_id,
                index_id=unreferenced_id,
                path="src/old.py",
                blob_sha="c" * 40,
                language="python",
                content_hash="d" * 64,
                size_bytes=1,
                line_count=1,
                imports_json="[]",
                status="indexed",
                has_syntax_errors=False,
            )
        )

    deleted = await index_tasks.cleanup_old_repository_indexes_async(
        sessions=sessions, cutoff=datetime.now(UTC) - timedelta(days=90)
    )
    assert deleted == 1
    async with sessions() as session:
        assert await session.get(RepositoryIndex, referenced_id) is not None
        assert await session.get(RepositoryIndex, unreferenced_id) is None
        assert await session.get(CodeFile, file_id) is None
