from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shichimimi_agent.security.path_policy import is_path_allowed

_CREDENTIAL_URL_RE = re.compile(r"https://[^@\s]*@")


def _scrub(text: str) -> str:
    return _CREDENTIAL_URL_RE.sub("https://***@", text)


@dataclass(frozen=True)
class WriteResult:
    path: Path
    repo: str | None
    pushed: bool
    commit_sha: str | None = None


class DocumentRepositoryWriter:
    def __init__(self, root: Path, *, document_repositories: dict[str, Any] | None = None) -> None:
        self.root = root
        self.document_repositories = document_repositories or {}

    def write_dry_run(self, *, relative_path: str, content: str) -> WriteResult:
        output_path = self.root / ".data" / "dry-run" / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        return WriteResult(path=output_path, repo=None, pushed=False)

    def _find_repo_policy(self, repo: str) -> dict[str, Any] | None:
        for entry in self.document_repositories.values():
            if entry.get("repo") == repo:
                return entry
        return None

    def _run_git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            message = _scrub(f"git {' '.join(args)} failed: {stderr}")
            raise RuntimeError(message) from None

    def publish(
        self,
        *,
        repo: str,
        relative_path: str,
        content: str,
        commit_message: str,
        remote_url: str | None = None,
    ) -> WriteResult:
        repo_policy = self._find_repo_policy(repo)
        allowed = list((repo_policy or {}).get("allowed_paths") or [])
        denied = list((repo_policy or {}).get("denied_paths") or [])
        decision = is_path_allowed(relative_path, allowed=allowed, denied=denied)
        if not decision.allowed:
            raise PermissionError(f"path {relative_path!r} not permitted for repo {repo!r}: {decision.reason}")

        checkout_dir = self.root / ".data" / "notes-repo" / repo.split("/")[-1]
        remote = remote_url or f"https://github.com/{repo}.git"

        if not (checkout_dir / ".git").exists():
            checkout_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run_git("clone", remote, str(checkout_dir), cwd=checkout_dir.parent)
        else:
            self._run_git("pull", "--ff-only", cwd=checkout_dir)

        target_path = checkout_dir / relative_path
        resolved_checkout = checkout_dir.resolve(strict=False)
        resolved_target = target_path.resolve(strict=False)
        if not resolved_target.is_relative_to(resolved_checkout):
            raise PermissionError(f"path {relative_path!r} escapes checkout directory for repo {repo!r}")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

        self._run_git("add", "--", relative_path, cwd=checkout_dir)
        status = self._run_git("status", "--porcelain", cwd=checkout_dir)
        if not status.stdout.strip():
            return WriteResult(path=target_path, repo=repo, pushed=False, commit_sha=None)

        full_message = f"{commit_message}\n\nGenerated-by: 7mimi-agent"
        self._run_git("commit", "-m", full_message, cwd=checkout_dir)
        self._run_git("push", cwd=checkout_dir)
        commit_sha = self._run_git("rev-parse", "HEAD", cwd=checkout_dir).stdout.strip()
        return WriteResult(path=target_path, repo=repo, pushed=True, commit_sha=commit_sha)
