"""Static checks on imports: which top-level modules the project code needs, and which
ones a built tree provides."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
# Values that differ between interpreters: a static scan cannot tell which branch runs.
_INTERPRETER_FACTS = {("sys", "version_info"), ("sys", "platform"), ("os", "name")}
_NON_MODULE_DIRS = {"__pycache__", "bin"}


def _name(node: ast.expr) -> str:
    """``x`` for ``x`` and for ``a.b.x``, empty for anything else."""
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_name(node) in _IMPORT_ERRORS for node in types)


def _suppresses_import_error(item: ast.withitem) -> bool:
    """``with suppress(ImportError):`` or ``with contextlib.suppress(...)``."""
    call = item.context_expr
    return (
        isinstance(call, ast.Call)
        and _name(call.func) == "suppress"
        and any(_name(arg) in _IMPORT_ERRORS for arg in call.args)
    )


def _depends_on_interpreter(test: ast.expr) -> bool:
    """``sys.version_info >= (3, 11)``, ``sys.platform == "win32"``, ``os.name == "nt"``..."""
    return any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id, node.attr) in _INTERPRETER_FACTS
        for node in ast.walk(test)
    )


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

    def _visit_all(self, statements: Iterable[ast.AST], *, guarded: bool) -> None:
        self.guarded += guarded
        for statement in statements:
            self.visit(statement)
        self.guarded -= guarded

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_all(node.body, guarded=any(_catches_import_error(h) for h in node.handlers))
        self._visit_all([*node.orelse, *node.finalbody, *node.handlers], guarded=False)

    visit_TryStar = visit_Try  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        self._visit_all(node.body, guarded=any(_suppresses_import_error(i) for i in node.items))

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        if _depends_on_interpreter(node.test):
            self._visit_all([*node.body, *node.orelse], guarded=True)
            return
        self._visit_all(node.body, guarded=_is_type_checking(node.test))
        self._visit_all(node.orelse, guarded=False)


def scan_imports(roots: Iterable[Path]) -> dict[str, list[str]]:
    """Map each unconditionally imported top-level module to where it is imported.

    Ignored: imports guarded by ``try/except ImportError``, ``contextlib.suppress(ImportError)``
    or ``if TYPE_CHECKING:``, imports in either branch of a test on ``sys.version_info``,
    ``sys.platform`` or ``os.name``, and relative imports.
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
    if not root.is_dir():
        return []
    names = {_module_name(entry) for entry in root.iterdir()}
    return sorted(name for name in names if name is not None and name.isidentifier())


def _module_name(entry: Path) -> str | None:
    """The module an entry of a flat tree provides, if any (not checked as an identifier)."""
    name = entry.name
    if entry.is_dir():
        is_metadata = name.endswith((".dist-info", ".data", ".libs", ".egg-info"))
        return None if is_metadata or name in _NON_MODULE_DIRS else name
    if name.endswith(".py"):
        return name.removesuffix(".py")
    if name.endswith(".so") or ".so." in name:
        return name.split(".")[0]  # _speedups.cpython-314-x86_64-linux-gnu.so -> _speedups
    return None
