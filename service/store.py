"""Agent versions as commits in the agent repo, a git clone the operator places at AGENT_REPO.

Ref `refs/jobs/<job_id>/<index>` keeps each iteration's commit, so `git gc` never deletes it.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from service.config import AGENT_REPO, OPTIMIZER_MODEL

# The meta-agent's model authors each change. The service commits it.
OPTIMIZER_IDENTITY = {
    "GIT_AUTHOR_NAME": OPTIMIZER_MODEL, "GIT_AUTHOR_EMAIL": "optimizer@localhost",
    "GIT_COMMITTER_NAME": "optimizer", "GIT_COMMITTER_EMAIL": "optimizer@localhost",
}


class GitError(Exception):
    pass


def _git(*args: str, cwd: Path = AGENT_REPO, env: dict | None = None) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, check=True, timeout=120,
            env={**os.environ, **(env or {})},
        ).stdout
    except subprocess.CalledProcessError as e:
        raise GitError(e.stderr.strip()) from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} timed out") from e


def iteration_ref(job_id, index: int) -> str:
    return f"refs/jobs/{job_id}/{index}"


def pin(ref: str, dest_ref: str) -> str:
    """Point `dest_ref` at the commit `ref` names. Return the commit hash."""
    commit = _git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    _git("update-ref", dest_ref, commit)
    return commit


def export(commit: str, dest: Path) -> None:
    """Write the files of `commit` to `dest`."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp, "agent.tar")
        _git("archive", "--output", str(archive), commit)
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")


@contextmanager
def worktree(commit: str) -> Generator[Path]:
    """A temporary checkout of `commit`. It shares the repo's commits and refs."""
    tmp = tempfile.mkdtemp()
    path = Path(tmp, "agent")
    _git("worktree", "add", "--detach", "--quiet", str(path), commit)
    try:
        yield path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _git("worktree", "prune")


def head(path: Path) -> str:
    """The commit checked out at `path`."""
    return _git("rev-parse", "HEAD", cwd=path).strip()


def reset_worktree(path: Path, commit: str) -> None:
    """Make the checkout at `path` match `commit` exactly, dropping every change and untracked file."""
    _git("checkout", "--quiet", "--detach", "--force", commit, cwd=path)
    _git("clean", "-fdxq", cwd=path)


def commit_worktree(path: Path, message: str, dest_ref: str) -> tuple[str, str] | None:
    """Commit the files in the checkout at `path` on top of its HEAD as `dest_ref`.

    Return (new commit, parent), or None when the files match HEAD. `__pycache__` directories are never committed.
    """
    parent = head(path)
    _git("add", "--all", "--", ".", ":(exclude,glob)**/__pycache__/**", cwd=path)
    tree = _git("write-tree", cwd=path).strip()
    if tree == _git("rev-parse", f"{parent}^{{tree}}").strip():
        return None
    commit = _git("commit-tree", tree, "-p", parent, "-m", message, env=OPTIMIZER_IDENTITY).strip()
    _git("update-ref", dest_ref, commit)
    return commit, parent


def message(commit: str) -> str:
    return _git("log", "-1", "--format=%B", commit).strip()


def diff(commit: str) -> str:
    """Diff from the commit's parent."""
    return _git("diff", f"{commit}^", commit)
