"""Static checks on imports: which top-level modules the project code needs, and which
ones a built tree provides."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
_NON_MODULE_DIRS = {"__pycache__", "bin"}


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names: list[ast.expr] = (
        list(handler.type.elts) if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for node in names:
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        if name in _IMPORT_ERRORS:
            return True
    return False


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


class _Collector(ast.NodeVisitor):
    def __init__(self, location: str) -> None:
        self.location = location
        self.guarded = 0
        self.found: dict[str, list[str]] = {}

    def _add(self, module: str, node: ast.stmt) -> None:
        if not self.guarded:
            top = module.split(".", maxsplit=1)[0]
            self.found.setdefault(top, []).append(f"{self.location}:{node.lineno}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._add(node.module, node)

    def visit_Try(self, node: ast.Try) -> None:
        guarded = any(_catches_import_error(h) for h in node.handlers)
        self.guarded += guarded
        for stmt in node.body:
            self.visit(stmt)
        self.guarded -= guarded
        for stmt in [*node.orelse, *node.finalbody]:
            self.visit(stmt)
        for handler in node.handlers:
            self.visit(handler)

    visit_TryStar = visit_Try  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        guarded = _is_type_checking(node.test)
        self.guarded += guarded
        for stmt in node.body:
            self.visit(stmt)
        self.guarded -= guarded
        for stmt in node.orelse:
            self.visit(stmt)


def scan_imports(roots: Iterable[Path]) -> dict[str, list[str]]:
    """Map each unconditionally imported top-level module to where it is imported.

    Imports inside ``try/except ImportError`` or ``if TYPE_CHECKING:`` are ignored,
    as are relative imports.
    """
    found: dict[str, list[str]] = {}
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            try:
                tree = ast.parse(path.read_bytes(), filename=str(path))
            except (SyntaxError, ValueError):
                continue
            location = path.name if root.is_file() else path.relative_to(root).as_posix()
            collector = _Collector(location)
            collector.visit(tree)
            for module, places in collector.found.items():
                found.setdefault(module, []).extend(places)
    return found


def top_level_modules(root: Path) -> list[str]:
    """Importable top-level names of a flat tree (a ``--target`` install or code dir)."""
    names: set[str] = set()
    if not root.is_dir():
        return []
    for entry in root.iterdir():
        name = entry.name
        if entry.is_dir():
            if (
                name in _NON_MODULE_DIRS
                or name.endswith((".dist-info", ".data", ".libs", ".egg-info"))
                or not name.isidentifier()
            ):
                continue
            names.add(name)
        elif name.endswith(".py") and name[:-3].isidentifier():
            names.add(name[:-3])
        elif name.endswith(".so") or ".so." in name:
            stem = name.split(".")[0]
            if stem.isidentifier():
                names.add(stem)
    return sorted(names)
