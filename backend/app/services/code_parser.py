"""Bounded, syntax-aware extraction of repository symbols and imports.

This module parses source as data only. It never imports, evaluates, or executes it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Protocol

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

DEFAULT_MAX_SOURCE_BYTES = 1_000_000
MAX_SIGNATURE_CHARS = 500
MAX_DOCSTRING_CHARS = 500


@dataclass(frozen=True)
class ParsedSymbol:
    symbol_key: str
    kind: str
    name: str
    qualified_name: str
    signature: str
    docstring_summary: str | None
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    exported: bool = False


@dataclass(frozen=True)
class ParsedImport:
    statement: str
    module: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ParsedFile:
    path: str
    language: str | None
    status: str
    content_hash: str | None
    size_bytes: int
    line_count: int
    has_syntax_errors: bool
    symbols: tuple[ParsedSymbol, ...] = ()
    imports: tuple[ParsedImport, ...] = ()
    skip_reason: str | None = None


class CodeParser(Protocol):
    def parse(self, path: str, source: bytes) -> ParsedFile: ...


@dataclass(frozen=True)
class _Grammar:
    language: str
    grammar: Language
    symbol_types: frozenset[str]
    import_types: frozenset[str]
    python_style_imports: bool = False


def _grammars() -> dict[str, _Grammar]:
    return {
        ".py": _Grammar(
            "python",
            Language(tree_sitter_python.language()),
            frozenset({"function_definition", "class_definition"}),
            frozenset({"import_statement", "import_from_statement"}),
            python_style_imports=True,
        ),
        ".js": _Grammar(
            "javascript",
            Language(tree_sitter_javascript.language()),
            frozenset(
                {
                    "function_declaration",
                    "function_signature",
                    "generator_function_declaration",
                    "class_declaration",
                    "method_definition",
                    "interface_declaration",
                    "type_alias_declaration",
                    "enum_declaration",
                    "lexical_declaration",
                }
            ),
            frozenset({"import_statement"}),
        ),
        ".jsx": _Grammar(
            "javascript",
            Language(tree_sitter_javascript.language()),
            frozenset(
                {
                    "function_declaration",
                    "function_signature",
                    "generator_function_declaration",
                    "class_declaration",
                    "method_definition",
                    "interface_declaration",
                    "type_alias_declaration",
                    "enum_declaration",
                    "lexical_declaration",
                }
            ),
            frozenset({"import_statement"}),
        ),
        ".mjs": _Grammar(
            "javascript",
            Language(tree_sitter_javascript.language()),
            frozenset(
                {
                    "function_declaration",
                    "generator_function_declaration",
                    "class_declaration",
                    "method_definition",
                }
            ),
            frozenset({"import_statement"}),
        ),
        ".cjs": _Grammar(
            "javascript",
            Language(tree_sitter_javascript.language()),
            frozenset(
                {
                    "function_declaration",
                    "generator_function_declaration",
                    "class_declaration",
                    "method_definition",
                }
            ),
            frozenset({"import_statement"}),
        ),
        ".ts": _Grammar(
            "typescript",
            Language(tree_sitter_typescript.language_typescript()),
            frozenset(
                {
                    "function_declaration",
                    "function_signature",
                    "generator_function_declaration",
                    "class_declaration",
                    "method_definition",
                    "interface_declaration",
                    "type_alias_declaration",
                    "enum_declaration",
                    "lexical_declaration",
                }
            ),
            frozenset({"import_statement"}),
        ),
        ".tsx": _Grammar(
            "typescript",
            Language(tree_sitter_typescript.language_tsx()),
            frozenset(
                {
                    "function_declaration",
                    "function_signature",
                    "generator_function_declaration",
                    "class_declaration",
                    "method_definition",
                    "interface_declaration",
                    "type_alias_declaration",
                    "enum_declaration",
                    "lexical_declaration",
                }
            ),
            frozenset({"import_statement"}),
        ),
    }


def _text(source: bytes, start: int, end: int, limit: int | None = None) -> str:
    value = source[start:end].decode("utf-8", errors="replace").strip()
    return value[:limit] if limit is not None else value


def _line(node, *, end: bool = False) -> int:
    point = node.end_point if end else node.start_point
    return point.row + 1


def _symbol_name(node, source: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(source, name.start_byte, name.end_byte, 200)
    if node.type == "method_definition":
        name = node.child_by_field_name("name")
        if name is not None:
            return _text(source, name.start_byte, name.end_byte, 200)
    if node.type == "lexical_declaration":
        declarator = next(
            (child for child in node.named_children if child.type == "variable_declarator"), None
        )
        if declarator is not None:
            value = declarator.child_by_field_name("value")
            name = declarator.child_by_field_name("name")
            if (
                value is not None
                and value.type in {"arrow_function", "function_expression"}
                and name
            ):
                return _text(source, name.start_byte, name.end_byte, 200)
    return None


def _node_kind(node_type: str) -> str:
    return {
        "function_definition": "function",
        "function_declaration": "function",
        "function_signature": "function",
        "generator_function_declaration": "function",
        "function_expression": "function",
        "arrow_function": "function",
        "class_definition": "class",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    }.get(node_type, "function")


def _is_exported(node) -> bool:
    current = node
    while current.parent is not None and current.parent.type in {
        "export_statement",
        "export_declaration",
    }:
        if current.parent.type in {"export_statement", "export_declaration"}:
            return True
        current = current.parent
    return False


def _exported_names(root, source: bytes) -> set[str]:
    exported: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "export_specifier":
            name = node.child_by_field_name("name")
            if name is None and node.named_children:
                name = node.named_children[0]
            if name is not None:
                exported.add(_text(source, name.start_byte, name.end_byte, 200))
        stack.extend(reversed(node.named_children))
    return exported


def _docstring(node, source: bytes) -> str | None:
    body = node.child_by_field_name("body")
    if body is None:
        body = next((child for child in node.named_children if child.type == "block"), None)
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        first = first.named_children[0]
    if first.type not in {"string", "concatenated_string", "template_string"}:
        return None
    value = _text(source, first.start_byte, first.end_byte, MAX_DOCSTRING_CHARS)
    return value or None


def _signature(node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is None:
        body = next((child for child in node.named_children if child.type == "block"), None)
    end = body.start_byte if body is not None else node.end_byte
    return _text(source, node.start_byte, end, MAX_SIGNATURE_CHARS)


def _python_import(node, source: bytes) -> tuple[str, str | None]:
    statement = _text(source, node.start_byte, node.end_byte, 1000)
    match = re.match(r"(?:from\s+([\w.]+)\s+import|import\s+([\w., *]+))", statement)
    return statement, (match.group(1) or match.group(2)).strip() if match else None


def _module_import(node, source: bytes) -> tuple[str, str | None]:
    statement = _text(source, node.start_byte, node.end_byte, 1000)
    string_node = next(
        (child for child in node.named_children if child.type in {"string", "string_fragment"}),
        None,
    )
    module = None
    if string_node is not None:
        module = _text(source, string_node.start_byte, string_node.end_byte, 500).strip("\"'` ")
    return statement, module


class TreeSitterCodeParser:
    """Extract a deterministic symbol/import inventory for supported source files."""

    def __init__(self, *, max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES) -> None:
        self.max_source_bytes = max(1, max_source_bytes)
        self._grammars = _grammars()

    def parse(self, path: str, source: bytes) -> ParsedFile:
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        grammar = self._grammars.get(suffix)
        size = len(source)
        if grammar is None:
            return ParsedFile(
                path, None, "unsupported", None, size, 0, False, skip_reason="unsupported_language"
            )
        if size > self.max_source_bytes:
            return ParsedFile(
                path,
                grammar.language,
                "oversized",
                None,
                size,
                0,
                False,
                skip_reason="source_too_large",
            )
        if b"\x00" in source:
            return ParsedFile(
                path, grammar.language, "binary", None, size, 0, False, skip_reason="binary_content"
            )
        try:
            source.decode("utf-8")
        except UnicodeDecodeError:
            return ParsedFile(
                path,
                grammar.language,
                "skipped",
                None,
                size,
                0,
                False,
                skip_reason="invalid_encoding",
            )

        parser = Parser(grammar.grammar)
        tree = parser.parse(source)
        symbols: list[ParsedSymbol] = []
        imports: list[ParsedImport] = []
        explicit_exports = _exported_names(tree.root_node, source)
        stack = [(tree.root_node, ())]
        while stack:
            node, parent_scope = stack.pop()
            is_symbol = node.type in grammar.symbol_types
            name = _symbol_name(node, source) if is_symbol else None
            scope = (*parent_scope, name) if is_symbol and name else parent_scope
            if is_symbol and name:
                qualified_name = ".".join(scope)
                signature = _signature(node, source)
                symbols.append(
                    ParsedSymbol(
                        symbol_key="",
                        kind=_node_kind(node.type),
                        name=name,
                        qualified_name=qualified_name,
                        signature=signature,
                        docstring_summary=_docstring(node, source),
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                        start_line=_line(node),
                        end_line=_line(node, end=True),
                        exported=_is_exported(node)
                        or (len(scope) == 1 and name in explicit_exports),
                    )
                )

            if node.type in grammar.import_types:
                statement, module = (
                    _python_import(node, source)
                    if grammar.python_style_imports
                    else _module_import(node, source)
                )
                imports.append(ParsedImport(statement, module, _line(node), _line(node, end=True)))

            stack.extend((child, scope) for child in reversed(node.named_children))

        signatures_seen: Counter[tuple[str, str, str]] = Counter()
        keyed_symbols: list[ParsedSymbol] = []
        for symbol in symbols:
            signature_hash = hashlib.sha256(symbol.signature.encode("utf-8")).hexdigest()[:12]
            base_key = (symbol.kind, symbol.qualified_name, signature_hash)
            signatures_seen[base_key] += 1
            suffix = f"#{signatures_seen[base_key]}" if signatures_seen[base_key] > 1 else ""
            keyed_symbols.append(
                replace(
                    symbol,
                    symbol_key=f"{symbol.kind}:{symbol.qualified_name}:{signature_hash}{suffix}",
                )
            )
        text = source.decode("utf-8")
        return ParsedFile(
            path=path,
            language=grammar.language,
            status="parsed",
            content_hash=hashlib.sha256(source).hexdigest(),
            size_bytes=size,
            line_count=len(text.splitlines()),
            has_syntax_errors=tree.root_node.has_error,
            symbols=tuple(keyed_symbols),
            imports=tuple(imports),
        )
