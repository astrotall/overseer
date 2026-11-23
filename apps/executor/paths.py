from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import PureWindowsPath
from typing import Final

from apps.executor.consumer import RequestRefusedError
from apps.executor.excel.validation import validate_workbook_path
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

logger = get_logger(__name__)

Resolver = Callable[[str], str]

DRIVE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]:")
FORBIDDEN_CHARS: Final[frozenset[str]] = frozenset('<>:"/\\|?*') | frozenset(
    chr(code) for code in range(32)
)
RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"{device}{suffix}" for device in ("COM", "LPT") for suffix in "123456789¹²³"}
)
EXTENDED_PREFIX: Final = "\\\\?\\"
EXTENDED_UNC_PREFIX: Final = "\\\\?\\UNC\\"
DEFAULT_FOLDER: Final[tuple[str, ...]] = ("Documents", "Overseer")

NO_PROFILE_TEXT: Final = (
    "Не найден профиль пользователя Windows (USERPROFILE): задайте разрешённые папки явно "
    "в EXECUTOR_ALLOWED_ROOTS"
)
BAD_ROOT_TEXT: Final = (
    "EXECUTOR_ALLOWED_ROOTS: элемент {index} — не локальная папка вида C:\\...: сетевые, "
    "device- и относительные пути, «..», имена устройств и альтернативные потоки запрещены"
)
MISSING_ROOT_TEXT: Final = "EXECUTOR_ALLOWED_ROOTS: элемент {index} не существует или не папка"
NO_ROOTS_TEXT: Final = "Нужна хотя бы одна разрешённая папка"


class PathNotAllowedError(RequestRefusedError):
    def __init__(self, reason: str) -> None:
        super().__init__("path_not_allowed")
        self.reason = reason


def local_path(path: str) -> PureWindowsPath | None:
    if not isinstance(path, str) or not path or path.replace("/", "\\").startswith("\\\\"):
        return None
    pure = PureWindowsPath(path)
    if not DRIVE.fullmatch(pure.drive) or pure.root != "\\":
        return None
    for part in pure.parts[1:]:
        if part == ".." or part != part.rstrip(" ."):
            return None
        if any(char in FORBIDDEN_CHARS for char in part):
            return None
        if part.partition(".")[0].rstrip(" ").upper() in RESERVED_NAMES:
            return None
    return pure


def strip_extended_prefix(resolved: str) -> str | None:
    if resolved.upper().startswith(EXTENDED_UNC_PREFIX.upper()):
        return None
    if resolved.startswith(EXTENDED_PREFIX):
        return resolved[len(EXTENDED_PREFIX) :]
    return resolved


def default_root(environ: Mapping[str, str]) -> PureWindowsPath:
    profile = local_path(environ.get("USERPROFILE", ""))
    if profile is None:
        raise ConfigurationError(NO_PROFILE_TEXT)
    return profile.joinpath(*DEFAULT_FOLDER)


def platform_resolver() -> Resolver | None:
    return os.path.realpath if sys.platform == "win32" else None


class PathPolicy:
    def __init__(
        self, roots: Iterable[str | PureWindowsPath], *, resolver: Resolver | None = None
    ) -> None:
        checked: list[PureWindowsPath] = []
        resolved: list[PureWindowsPath] = []
        for index, root in enumerate(roots):
            pure = local_path(str(root))
            real = pure if pure is None or resolver is None else _resolve(resolver, str(pure))
            if pure is None or real is None:
                raise ConfigurationError(BAD_ROOT_TEXT.format(index=index))
            checked.append(pure)
            resolved.append(real)
        if not checked:
            raise ConfigurationError(NO_ROOTS_TEXT)

        self._resolver = resolver
        self.roots: tuple[PureWindowsPath, ...] = tuple(checked)
        self._resolved_roots: tuple[PureWindowsPath, ...] = tuple(resolved)

    def check(self, path: str) -> str:
        if local_path(path) is None:
            raise PathNotAllowedError("shape")
        normalized = validate_workbook_path(path)
        pure = PureWindowsPath(normalized)
        if not _inside(pure, self.roots):
            raise PathNotAllowedError("outside")
        if self._resolver is not None:
            real = _resolve(self._resolver, normalized)
            if real is None or not _inside(real, self._resolved_roots):
                raise PathNotAllowedError("escapes_after_resolution")
        return normalized

    def require_existing(self) -> None:
        for index, root in enumerate(self.roots):
            if not os.path.isdir(str(root)):
                raise ConfigurationError(MISSING_ROOT_TEXT.format(index=index))


def build_path_policy(
    configured: list[str],
    *,
    environ: Mapping[str, str] = os.environ,
    resolver: Resolver | None = None,
    on_windows: bool = sys.platform == "win32",
) -> PathPolicy:
    roots: list[str | PureWindowsPath] = list(configured) or [default_root(environ)]
    if on_windows and not configured:
        os.makedirs(str(roots[0]), exist_ok=True)
    policy = PathPolicy(roots, resolver=resolver)
    if on_windows:
        policy.require_existing()
    logger.info("executor.allowed_roots", count=len(policy.roots), default=not configured)
    return policy


def _resolve(resolver: Resolver, path: str) -> PureWindowsPath | None:
    resolved = strip_extended_prefix(resolver(path))
    return None if resolved is None else local_path(resolved)


def _inside(path: PureWindowsPath, roots: Iterable[PureWindowsPath]) -> bool:
    return any(path != root and path.is_relative_to(root) for root in roots)
