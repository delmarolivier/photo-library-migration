"""Safety rails that keep the tool away from real user media.

* ``PathGuard``: a PEP 578 audit hook that blocks file access to protected paths
  (``~/Pictures``, any ``*.photoslibrary`` outside scratch space, real Takeout exports).
  The pytest plugin ``photomigrate.testing.pytest_guard`` activates one for every test
  session (CLAUDE.md rule 4).
* ``find_forbidden_calls`` / ``main``: a static check that production code never calls a delete
  or trash API (CLAUDE.md rule 3). CI runs it as ``photomigrate-no-delete-check src``.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import pwd
import re
import shlex
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePath
from typing import Any

PathArg = str | bytes | os.PathLike[str] | os.PathLike[bytes]

ANNOUNCE_EVENT = "photomigrate.path"
"""Audit event for paths handed to an external tool over a pipe (see ``announce_path``)."""

_MEDIA_STORE_PATTERNS = ("*.photoslibrary", "takeout", "takeout-*.zip", "takeout-*.tgz")


class ForbiddenPathError(PermissionError):
    """Raised when guarded code touches a protected path."""


@dataclass(frozen=True)
class Violation:
    event: str
    path: str


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _forms(path: str) -> tuple[str, str]:
    lexical = os.path.normpath(os.path.abspath(path))
    return lexical, os.path.realpath(lexical)


def _root_forms(roots: Iterable[Path]) -> tuple[str, ...]:
    return tuple(form for root in roots for form in _forms(os.fspath(root)))


def _is_media_store(part: str) -> bool:
    name = part.casefold()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _MEDIA_STORE_PATTERNS)


@dataclass(frozen=True)
class GuardRules:
    """Which paths count as real user media.

    Anything under ``forbidden_roots`` is always protected. Photos libraries and Takeout
    exports are protected unless they live under ``allowed_roots`` (tmp, test fixtures).
    Absolute paths are checked both as written and with symlinks resolved.
    """

    forbidden_roots: tuple[Path, ...]
    allowed_roots: tuple[Path, ...]

    @classmethod
    def for_current_user(
        cls, allowed_roots: Iterable[Path] = (), extra_forbidden: Iterable[Path] = ()
    ) -> GuardRules:
        homes = {Path(pwd.getpwuid(os.getuid()).pw_dir), Path.home()}
        pictures = tuple(sorted(home / "Pictures" for home in homes))
        return cls(
            forbidden_roots=(*pictures, *extra_forbidden), allowed_roots=tuple(allowed_roots)
        )

    @cached_property
    def _forbidden(self) -> tuple[str, ...]:
        return _root_forms(self.forbidden_roots)

    @cached_property
    def _allowed(self) -> tuple[str, ...]:
        return _root_forms(self.allowed_roots)

    def is_forbidden(self, path: PathArg) -> bool:
        text = os.fsdecode(path)
        if os.path.isabs(text):
            return any(self._form_is_forbidden(form) for form in _forms(text))
        # A relative name may be relative to a directory fd (shutil.rmtree, os.fwalk) rather
        # than the cwd, and that fd was checked when it was opened. So judge the cwd itself,
        # plus the always-forbidden roots.
        return self.is_forbidden(os.getcwd()) or any(
            self._under_forbidden_root(form) for form in _forms(text)
        )

    def _under_forbidden_root(self, path: str) -> bool:
        return any(_within(path, root) for root in self._forbidden)

    def _form_is_forbidden(self, path: str) -> bool:
        if self._under_forbidden_root(path):
            return True
        if any(_within(path, root) for root in self._allowed):
            return False
        return any(_is_media_store(part) for part in PurePath(path).parts)


def _is_pathlike(value: object) -> bool:
    return isinstance(value, str | bytes | os.PathLike)


def _path_args(args: Iterable[object]) -> Iterator[str]:
    for arg in args:
        if _is_pathlike(arg):
            path = os.fsdecode(arg)  # type: ignore[arg-type]
            if path.startswith("file:"):  # sqlite3 URI form
                path = path.removeprefix("file:").split("?", 1)[0]
            yield path


def _command_tokens(command: object) -> Iterator[str]:
    if isinstance(command, str | bytes):
        text = os.fsdecode(command)
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
    elif isinstance(command, list | tuple):
        parts = list(_path_args(command))
    else:
        parts = []
    for part in parts:
        yield part
        if part.startswith("~"):
            yield os.path.expanduser(part)
        if "=" in part:
            yield part.split("=", 1)[1]


_FS_EVENTS = frozenset({"open", "sqlite3.connect", ANNOUNCE_EVENT})
_FS_EVENT_PREFIXES = ("os.", "shutil.", "glob.", "pathlib.", "tempfile.")
_NON_FS_OS_EVENTS = frozenset({"os.putenv", "os.unsetenv"})


def _candidate_paths(event: str, args: tuple[Any, ...]) -> Iterator[str]:
    if event == "subprocess.Popen":
        executable, argv, cwd, _env = args
        yield from _path_args((executable, cwd))
        yield from _command_tokens(argv)
    elif event == "os.system":
        yield from _command_tokens(args[0])
    elif event in {"os.exec", "os.posix_spawn"}:
        yield from _path_args(args[:1])
        yield from _command_tokens(args[1])
    elif event == "os.spawn":
        yield from _path_args(args[1:2])
        yield from _command_tokens(args[2])
    elif event in _FS_EVENTS or (
        event.startswith(_FS_EVENT_PREFIXES) and event not in _NON_FS_OS_EVENTS
    ):
        yield from _path_args(args)


class PathGuard:
    """Blocks and records access to protected paths while active."""

    def __init__(self, rules: GuardRules) -> None:
        self.rules = rules
        self.violations: list[Violation] = []

    def check(self, event: str, args: tuple[Any, ...]) -> None:
        for path in _candidate_paths(event, args):
            if self.rules.is_forbidden(path):
                self.violations.append(Violation(event=event, path=path))
                raise ForbiddenPathError(
                    f"photomigrate safety guard blocked {event} on {path!r}: "
                    "tests must never touch real user media (CLAUDE.md rule 4)"
                )

    def activate(self) -> None:
        """Start handling audit events. The most recently activated guard wins."""
        _install_audit_hook()
        _active_guards.append(self)

    def deactivate(self) -> None:
        if self in _active_guards:
            _active_guards.remove(self)


_active_guards: list[PathGuard] = []
_hook_installed = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _active_guards:
        _active_guards[-1].check(event, args)


def _install_audit_hook() -> None:
    # Audit hooks cannot be removed, so install one dispatcher for the life of the process.
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True


def announce_path(path: PathArg) -> None:
    """Declare a path that is about to reach an external tool without appearing in its argv.

    PyExifTool's stay-open mode sends paths over stdin, which no audit event sees. Call this
    first so an active guard can block it.
    """
    sys.audit(ANNOUNCE_EVENT, os.fsdecode(path))


# --- No-delete static check (CLAUDE.md rule 3) -------------------------------------------

_DENY_WORDS = frozenset(
    {"delete", "trash", "unlink", "rmtree", "rmdir", "removedirs", "send2trash"}
)
_REMOVE_OBJECTS = frozenset(
    {"asset", "assets", "photo", "photos", "media", "item", "items", "file", "files"}
)
_DENY_DOTTED = frozenset({"os.remove"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: delete API '{self.name}' is forbidden (CLAUDE.md rule 3)"


def _name_words(name: str) -> list[str]:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [word for word in snake.lower().split("_") if word]


def _is_denied_name(name: str) -> bool:
    words = _name_words(name)
    for i, word in enumerate(words):
        previous = words[i - 1] if i else ""
        following = words[i + 1] if i + 1 < len(words) else ""
        if word == "delete" and previous == "to":  # the "To Delete" staging album
            continue
        if word in _DENY_WORDS or (word == "remove" and following in _REMOVE_OBJECTS):
            return True
    return False


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _referenced_names(node: ast.AST) -> list[tuple[str, list[str]]]:
    """(dotted name, parts to check) for calls and imports."""
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        return [(dotted, [dotted.rsplit(".", 1)[-1]])] if dotted else []
    if isinstance(node, ast.Import):
        return [(alias.name, alias.name.split(".")) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = f"{node.module}." if node.module else ""
        return [(f"{module}{alias.name}", [alias.name]) for alias in node.names]
    return []


def find_forbidden_calls(source: str, filename: str = "<string>") -> list[Finding]:
    findings = [
        Finding(path=filename, line=getattr(node, "lineno", 0), name=dotted)
        for node in ast.walk(ast.parse(source, filename))
        for dotted, parts in _referenced_names(node)
        if dotted in _DENY_DOTTED or any(_is_denied_name(part) for part in parts)
    ]
    return sorted(findings, key=lambda finding: finding.line)


def scan_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for root in paths:
        for file in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
            findings.extend(find_forbidden_calls(file.read_text(encoding="utf-8"), str(file)))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``photomigrate-no-delete-check [PATH ...]`` (default: ``src``)."""
    args = sys.argv[1:] if argv is None else argv
    findings = scan_paths([Path(arg) for arg in args] or [Path("src")])
    for finding in findings:
        print(finding)
    if findings:
        print(f"no-delete check FAILED: {len(findings)} forbidden call(s)")
        return 1
    print("no-delete check passed")
    return 0
