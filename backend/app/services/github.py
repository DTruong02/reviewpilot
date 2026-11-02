import asyncio
import base64
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
import jwt
from jwt.exceptions import PyJWTError

from app.core.config import settings
from app.schemas.review import PullRequestFile

API_VERSION = "2026-03-10"
DEFAULT_TIMEOUT = 20.0
MAX_SOURCE_FILE_BYTES = 1_000_000
MAX_DIFF_RESPONSE_BYTES = 5_000_000
MAX_TREE_RESPONSE_BYTES = 8_000_000
_BINARY_SUFFIXES = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".so",
    ".sqlite",
    ".tar",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}
_GENERATED_PARTS = {
    ".git",
    "build",
    "dist",
    "generated",
    "node_modules",
    "vendor",
    "vendors",
}
_GENERATED_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock"}


class GitHubClientError(RuntimeError):
    def __init__(self, code: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


class InstallationTokenProvider(Protocol):
    async def get_token(self, installation_id: int) -> str: ...


@dataclass(frozen=True)
class GitTreeFile:
    path: str
    blob_sha: str
    size_bytes: int | None


def is_safe_index_path(path: str) -> bool:
    """Exclude generated, secret-bearing, and non-source paths before fetching content."""
    if not is_supported_source_path(path):
        return False
    parts = path.replace("\\", "/").lower().split("/")
    basename = parts[-1]
    if any(":" in part or "\x00" in part for part in parts) or ".ssh" in parts:
        return False
    if basename in {".env", ".npmrc", ".pypirc", "credentials", "secrets.json"}:
        return False
    if basename.startswith(".env.") or basename.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    if basename.startswith(("id_rsa", "id_ed25519", "private_key")):
        return False
    return True


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: datetime


class GitHubAppTokenProvider:
    """Creates installation tokens and caches them only until a safe pre-expiry margin."""

    def __init__(
        self,
        app_id: int,
        private_key_path: Path,
        http_client: httpx.AsyncClient,
        *,
        api_url: str = "https://api.github.com",
        cache_skew_seconds: int = 300,
        now=time.time,
    ) -> None:
        self._app_id = app_id
        self._private_key_path = private_key_path
        self._http = http_client
        self._api_url = api_url.rstrip("/")
        self._cache_skew = timedelta(seconds=max(1, cache_skew_seconds))
        self._now = now
        self._cache: dict[int, _CachedToken] = {}
        self._lock = asyncio.Lock()

    def _make_jwt(self) -> str:
        now = int(self._now())
        try:
            private_key = self._private_key_path.read_text(encoding="utf-8")
        except OSError:
            raise GitHubClientError("github_private_key_unavailable") from None
        try:
            return jwt.encode(
                {"iat": now - 30, "exp": now + 9 * 60, "iss": str(self._app_id)},
                private_key,
                algorithm="RS256",
            )
        except (ValueError, TypeError, PyJWTError):
            raise GitHubClientError("github_private_key_invalid") from None

    async def get_token(self, installation_id: int) -> str:
        if installation_id <= 0:
            raise GitHubClientError("invalid_installation_id")
        async with self._lock:
            cached = self._cache.get(installation_id)
            now = datetime.fromtimestamp(self._now(), tz=UTC)
            if cached and cached.expires_at - self._cache_skew > now:
                return cached.value

            response = await _request_with_retries(
                self._http,
                "POST",
                f"{self._api_url}/app/installations/{installation_id}/access_tokens",
                headers=_headers(self._make_jwt()),
                json={},
            )
            try:
                body = response.json()
                token = body["token"]
                expiry_raw = body["expires_at"]
                expiry = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
                now = datetime.fromtimestamp(self._now(), tz=UTC)
                if (
                    not isinstance(token, str)
                    or not token
                    or expiry.tzinfo is None
                    or expiry.astimezone(UTC) <= now + self._cache_skew
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                raise GitHubClientError(
                    "github_token_response_invalid", response.status_code
                ) from None
            self._cache[installation_id] = _CachedToken(token, expiry.astimezone(UTC))
            return token


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
    }


async def _request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str | int] | None = None,
    json: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_response_bytes: int = 256_000,
    max_response_error_code: str = "github_response_too_large",
    sleep=asyncio.sleep,
    jitter=random.random,
) -> httpx.Response:
    for attempt in range(3):
        try:
            async with client.stream(
                method, url, headers=headers, params=params, json=json, timeout=timeout
            ) as response:
                limited = response.status_code == 429 or (
                    response.status_code == 403
                    and (
                        response.headers.get("X-RateLimit-Remaining") == "0"
                        or response.headers.get("Retry-After") is not None
                    )
                )
                if limited or response.status_code >= 500:
                    if attempt == 2:
                        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                        code = "github_rate_limited" if limited else "github_unavailable"
                        raise GitHubClientError(code, response.status_code, retry_after)
                    wait = (
                        _parse_retry_after(response.headers.get("Retry-After")) if limited else None
                    )
                    await sleep(
                        min(
                            wait if wait is not None else 0.25 * (2**attempt) + jitter() * 0.15,
                            2.0,
                        )
                    )
                    continue
                if response.is_error:
                    code = (
                        "github_permission_denied"
                        if response.status_code in (401, 403)
                        else "github_request_failed"
                    )
                    raise GitHubClientError(code, response.status_code)

                content_length = response.headers.get("Content-Length")
                if (
                    content_length
                    and content_length.isdecimal()
                    and int(content_length) > max_response_bytes
                ):
                    raise GitHubClientError(max_response_error_code, response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise GitHubClientError(max_response_error_code, response.status_code)
                return httpx.Response(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=bytes(body),
                    request=response.request,
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 2:
                raise GitHubClientError("github_network_error") from None
            await sleep(min(0.25 * (2**attempt) + jitter() * 0.15, 1.0))
            continue
    raise GitHubClientError("github_unavailable")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 2.0))
    except ValueError:
        return None


def is_supported_source_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = normalized.lower().split("/")
    if normalized.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return False
    basename = parts[-1]
    if basename in _GENERATED_NAMES or any(part in _GENERATED_PARTS for part in parts):
        return False
    if (
        any(part.endswith(".generated") for part in parts)
        or ".generated." in basename
        or ".min." in basename
    ):
        return False
    return Path(basename).suffix.lower() not in _BINARY_SUFFIXES


class GitHubClient:
    def __init__(
        self,
        token_provider: InstallationTokenProvider,
        http_client: httpx.AsyncClient,
        *,
        api_url: str | None = None,
        max_files: int | None = None,
        max_changed_lines: int | None = None,
        max_source_file_bytes: int = MAX_SOURCE_FILE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._token_provider = token_provider
        self._http = http_client
        self._api_url = (api_url or settings.github_api_url).rstrip("/")
        self._max_files = max(1, min(max_files or settings.review_max_files, 25))
        self._max_changed_lines = max(
            1, min(max_changed_lines or settings.review_max_changed_lines, 100_000)
        )
        self._max_source_file_bytes = max(1, min(max_source_file_bytes, MAX_SOURCE_FILE_BYTES))
        self._timeout = timeout_seconds

    async def fetch_pull_request_head_sha(
        self, installation_id: int, owner: str, repository: str, pull_request_number: int
    ) -> str:
        if pull_request_number <= 0 or not owner or not repository:
            raise GitHubClientError("invalid_pull_request_reference")
        token = await self._token_provider.get_token(installation_id)
        url = (
            f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            f"/pulls/{pull_request_number}"
        )
        response = await _request_with_retries(
            self._http,
            "GET",
            url,
            headers=_headers(token),
            timeout=self._timeout,
            max_response_bytes=MAX_DIFF_RESPONSE_BYTES,
            max_response_error_code="github_response_too_large",
        )
        try:
            payload = response.json()
            sha = payload["head"]["sha"]
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise GitHubClientError(
                "github_pull_request_response_invalid", response.status_code
            ) from None
        return sha.lower()

    async def fetch_pull_request_files(
        self, installation_id: int, owner: str, repository: str, pull_request_number: int
    ) -> list[PullRequestFile]:
        if pull_request_number <= 0 or not owner or not repository:
            raise GitHubClientError("invalid_pull_request_reference")
        token = await self._token_provider.get_token(installation_id)
        files: list[PullRequestFile] = []
        changed_lines = 0
        page = 1
        page_size = min(100, self._max_files)
        while len(files) < self._max_files and changed_lines < self._max_changed_lines:
            url = (
                f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
                f"/pulls/{pull_request_number}/files"
            )
            response = await _request_with_retries(
                self._http,
                "GET",
                url,
                headers=_headers(token),
                params={"per_page": page_size, "page": page},
                timeout=self._timeout,
                max_response_bytes=MAX_DIFF_RESPONSE_BYTES,
                max_response_error_code="github_diff_response_too_large",
            )
            try:
                items = response.json()
                if not isinstance(items, list):
                    raise ValueError
                parsed = [PullRequestFile.model_validate(item) for item in items]
            except (ValueError, TypeError):
                raise GitHubClientError(
                    "github_files_response_invalid", response.status_code
                ) from None
            if not parsed:
                break
            for file in parsed:
                if not is_supported_source_path(file.path) or not file.patch:
                    continue
                file_lines = file.additions + file.deletions
                if changed_lines + file_lines > self._max_changed_lines:
                    # Exclude the whole file rather than return a misleading partial diff.
                    return files
                files.append(file)
                changed_lines += file_lines
                if len(files) >= self._max_files or changed_lines >= self._max_changed_lines:
                    break
            if len(parsed) < page_size:
                break
            page += 1
        return files

    async def fetch_file_content(
        self,
        installation_id: int,
        owner: str,
        repository: str,
        path: str,
        ref: str,
    ) -> str | None:
        if not is_supported_source_path(path):
            return None
        token = await self._token_provider.get_token(installation_id)
        encoded_path = quote(path, safe="/")
        url = (
            f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            f"/contents/{encoded_path}"
        )
        try:
            response = await _request_with_retries(
                self._http,
                "GET",
                url,
                headers=_headers(token),
                params={"ref": ref},
                timeout=self._timeout,
                max_response_bytes=self._max_source_file_bytes * 2 + 100_000,
            )
        except GitHubClientError as exc:
            # A moved, deleted, or stale PR path should not fail the whole review.
            if exc.status_code == 404:
                return None
            raise
        try:
            body = response.json()
            size = body.get("size")
            if (
                body.get("encoding") != "base64"
                or body.get("truncated") is True
                or not isinstance(size, int)
                or size < 0
                or size > self._max_source_file_bytes
            ):
                return None
            raw = base64.b64decode(body.get("content", ""), validate=False)
            if len(raw) > self._max_source_file_bytes or b"\x00" in raw:
                return None
            return raw.decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError, AttributeError):
            return None

    async def fetch_commit_tree(
        self,
        installation_id: int,
        owner: str,
        repository: str,
        commit_sha: str,
        *,
        max_entries: int = 100_000,
    ) -> list[GitTreeFile]:
        """Fetch a bounded immutable tree inventory; symlinks/submodules are never returned."""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha) or not owner or not repository:
            raise GitHubClientError("invalid_commit_reference")
        token = await self._token_provider.get_token(installation_id)
        url = (
            f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            f"/git/trees/{commit_sha.lower()}"
        )
        response = await _request_with_retries(
            self._http,
            "GET",
            url,
            headers=_headers(token),
            params={"recursive": "1"},
            timeout=self._timeout,
            max_response_bytes=MAX_TREE_RESPONSE_BYTES,
            max_response_error_code="github_tree_response_too_large",
        )
        try:
            body = response.json()
            entries = body["tree"]
            if body.get("truncated") is True or not isinstance(entries, list):
                raise ValueError
            if len(entries) > max_entries:
                raise GitHubClientError("index_tree_entry_limit", response.status_code)
            files: list[GitTreeFile] = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                path = entry.get("path")
                sha = entry.get("sha")
                mode = entry.get("mode")
                size = entry.get("size")
                if not isinstance(path, str) or not is_safe_index_path(path):
                    continue
                if mode not in {"100644", "100755"}:
                    continue
                if (
                    not isinstance(sha, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
                    or not isinstance(size, int)
                    or size < 0
                ):
                    raise ValueError
                files.append(GitTreeFile(path, sha.lower(), size))
            return files
        except GitHubClientError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GitHubClientError("github_tree_response_invalid", response.status_code) from None

    async def aclose(self) -> None:
        await self._http.aclose()


def build_github_client() -> GitHubClient:
    """Construct the real client from server settings; callers should close its HTTP client."""
    if not settings.github_app_id or not settings.github_app_private_key_path:
        raise GitHubClientError("github_app_not_configured")
    http = httpx.AsyncClient()
    tokens = GitHubAppTokenProvider(
        settings.github_app_id,
        settings.github_app_private_key_path,
        http,
        api_url=settings.github_api_url,
        cache_skew_seconds=settings.github_token_cache_skew_seconds,
    )
    return GitHubClient(http_client=http, token_provider=tokens)
