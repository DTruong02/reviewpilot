from pathlib import Path

import pytest

from app.services.code_parser import TreeSitterCodeParser

FIXTURES = Path(__file__).parent / "fixtures" / "code_parser"


def parse_fixture(name: str):
    return TreeSitterCodeParser().parse(name, (FIXTURES / name).read_bytes())


def test_python_symbols_imports_and_nested_qualified_names() -> None:
    result = parse_fixture("nested.py")

    assert result.status == "parsed"
    assert result.language == "python"
    assert result.content_hash and len(result.content_hash) == 64
    assert [item.module for item in result.imports] == ["os", "pathlib"]
    symbols = {item.qualified_name: item for item in result.symbols}
    assert {"Cache", "Cache.fetch", "Cache.fetch.normalize", "replacement"} <= symbols.keys()
    assert symbols["Cache"].kind == "class"
    assert symbols["Cache"].docstring_summary == '"""A small cache fixture."""'
    assert symbols["Cache.fetch.normalize"].start_line == 9
    assert symbols["Cache.fetch.normalize"].end_line == 10
    assert symbols["replacement"].symbol_key.startswith("function:replacement:")


def test_javascript_extracts_imports_functions_exports_and_renames() -> None:
    result = parse_fixture("sample.js")

    assert result.status == "parsed"
    assert result.language == "javascript"
    assert result.imports[0].module == "node:fs"
    symbols = {item.name: item for item in result.symbols}
    assert symbols["loadConfig"].exported
    assert symbols["normalize"].exported
    assert symbols["normalize"].kind == "function"

    renamed = TreeSitterCodeParser().parse(
        "sample.js", b"export function loadSettings(path) { return path; }\n"
    )
    assert renamed.symbols[0].symbol_key.startswith("function:loadSettings:")
    assert renamed.symbols[0].symbol_key != symbols["loadConfig"].symbol_key


def test_jsx_extension_uses_javascript_grammar() -> None:
    result = parse_fixture("component.jsx")

    assert result.status == "parsed"
    assert result.language == "javascript"
    assert result.imports[0].module == "react"
    assert result.symbols[0].name == "Component"
    assert result.symbols[0].exported


def test_export_lists_and_overloads_have_explicit_exports_and_unique_keys() -> None:
    result = parse_fixture("exports.ts")

    overloads = [symbol for symbol in result.symbols if symbol.name == "parse"]
    helpers = [symbol for symbol in result.symbols if symbol.name == "localHelper"]
    assert len(overloads) == 3
    assert all(symbol.exported for symbol in overloads)
    assert len({symbol.symbol_key for symbol in overloads}) == 3
    assert len(helpers) == 2
    assert helpers[0].exported
    assert not helpers[1].exported


def test_deeply_nested_source_does_not_use_python_recursion_for_tree_walk() -> None:
    nested = "function f(){" * 1200 + "return 1;" + "}" * 1200
    result = TreeSitterCodeParser().parse("deep.js", nested.encode())

    assert result.status == "parsed"


def test_typescript_tsx_and_malformed_source_are_supported() -> None:
    result = parse_fixture("sample.tsx")
    malformed = parse_fixture("malformed.ts")

    assert result.language == "typescript"
    assert result.imports[0].module == "react"
    assert {symbol.name for symbol in result.symbols} == {"Props", "Label"}
    assert all(symbol.exported for symbol in result.symbols)
    assert malformed.status == "parsed"
    assert malformed.has_syntax_errors


@pytest.mark.parametrize(
    ("path", "source", "limit", "status", "reason"),
    [
        ("data.json", b"{}", 10, "unsupported", "unsupported_language"),
        ("large.py", b"x" * 11, 10, "oversized", "source_too_large"),
        ("binary.py", b"x\x00y", 10, "binary", "binary_content"),
        ("invalid.py", b"\xff", 10, "skipped", "invalid_encoding"),
    ],
)
def test_unsupported_oversized_binary_and_invalid_encoding_are_skipped(
    path: str, source: bytes, limit: int, status: str, reason: str
) -> None:
    result = TreeSitterCodeParser(max_source_bytes=limit).parse(path, source)

    assert result.status == status
    assert result.skip_reason == reason
    assert not result.symbols
