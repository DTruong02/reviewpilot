import re
from collections.abc import Awaitable, Callable, Sequence

from app.core.config import settings
from app.schemas.review import PullRequestFile, ReviewContext, ReviewFile

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_REDACTIONS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret|client[_-]?secret)\b\s*[:=]\s*)"
        r"(['\"]?)[A-Za-z0-9_./+=:-]{8,}\2"
    ),
)

FileContentFetcher = Callable[[str], Awaitable[str | None]]


def added_line_numbers(patch: str) -> frozenset[int]:
    """Return new-side line numbers introduced by unified diff additions."""
    result: set[int] = set()
    new_line: int | None = None
    for line in patch.splitlines():
        header = _HUNK.match(line)
        if header:
            new_line = int(header.group(1))
            continue
        if new_line is None or line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result.add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith(" "):
            new_line += 1
    return frozenset(result)


def redact_obvious_secrets(text: str) -> str:
    for pattern in _REDACTIONS:
        if pattern.groups == 2:
            text = pattern.sub(r"\1\2[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def _context_windows(content: str, changed_lines: frozenset[int], radius: int) -> str:
    lines = content.splitlines()
    if not lines or not changed_lines:
        return ""
    spans: list[list[int]] = []
    for line_number in sorted(changed_lines):
        start = max(1, line_number - radius)
        end = min(len(lines), line_number + radius)
        if spans and start <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    blocks = []
    for start, end in spans:
        block = "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1))
        blocks.append(block)
    return "\n…\n".join(blocks)


class ContextBuilder:
    def __init__(
        self,
        *,
        max_files: int | None = None,
        max_changed_lines: int | None = None,
        max_context_chars: int | None = None,
        context_lines: int | None = None,
    ) -> None:
        self.max_files = max(1, min(max_files or settings.review_max_files, 25))
        self.max_changed_lines = max_changed_lines or settings.review_max_changed_lines
        self.max_context_chars = max_context_chars or settings.review_max_context_chars
        self.context_lines = max(10, context_lines or settings.review_context_lines)

    async def build(
        self,
        *,
        repository: str,
        pull_request_number: int,
        pull_request_title: str,
        head_sha: str,
        changed_files: Sequence[PullRequestFile],
        fetch_content: FileContentFetcher,
    ) -> ReviewContext:
        files: list[ReviewFile] = []
        total_changed = 0
        total_chars = 0
        for changed_file in changed_files:
            if len(files) >= self.max_files:
                break
            patch = changed_file.patch
            if not patch or len(patch) > 30000:
                continue
            added = added_line_numbers(patch)
            if not added:
                continue
            line_count = changed_file.additions + changed_file.deletions
            if line_count <= 0 or total_changed + line_count > self.max_changed_lines:
                continue
            source = await fetch_content(changed_file.path)
            if source is None:
                continue
            code_context = _context_windows(source, added, self.context_lines)
            patch = redact_obvious_secrets(patch)
            code_context = redact_obvious_secrets(code_context)
            remaining = self.max_context_chars - total_chars
            if remaining <= 0:
                break
            # Keep the complete diff for precise anchors; spend the rest on relevant context.
            if len(patch) > remaining:
                continue
            budgeted_context = code_context[: max(0, remaining - len(patch))]
            if not budgeted_context:
                continue
            try:
                review_file = ReviewFile(
                    file_path=changed_file.path,
                    patch=patch,
                    context=budgeted_context,
                    added_lines=added,
                    additions=changed_file.additions,
                    deletions=changed_file.deletions,
                )
            except ValueError:
                continue
            files.append(review_file)
            total_chars += len(patch) + len(budgeted_context)
            total_changed += line_count

        return ReviewContext(
            repository=repository,
            pull_request_number=pull_request_number,
            pull_request_title=redact_obvious_secrets(pull_request_title)[:1000],
            head_sha=head_sha,
            files=tuple(files),
        )
