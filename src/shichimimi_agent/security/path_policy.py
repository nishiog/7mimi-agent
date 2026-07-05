from __future__ import annotations

import posixpath
from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True)
class PathDecision:
    allowed: bool
    reason: str


def _norm(path: str) -> str:
    return posixpath.normpath(path).lstrip("/")


def is_path_allowed(path: str, *, allowed: list[str], denied: list[str]) -> PathDecision:
    if path.startswith("/") or path.startswith("\\"):
        return PathDecision(False, "path escapes repository root")

    normalized = _norm(path)
    if normalized == ".." or normalized.startswith("../") or "/../" in f"/{normalized}/":
        return PathDecision(False, "path escapes repository root")

    for pattern in denied:
        if fnmatch(normalized, pattern) or normalized == pattern.rstrip("/**"):
            return PathDecision(False, f"path denied by pattern {pattern}")
    if not allowed:
        return PathDecision(False, "no allowed paths configured")
    for pattern in allowed:
        if fnmatch(normalized, pattern) or normalized == pattern.rstrip("/**"):
            return PathDecision(True, "allowed")
    return PathDecision(False, "path not covered by allowed paths")
