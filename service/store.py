"""Agent versions as commits in the agent repo, a git clone the operator places at AGENT_REPO.

Ref `refs/jobs/<job_id>/<index>` keeps each iteration's commit, so `git gc` never deletes it.
"""

import os
import subprocess
import tempfile
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


def pin_ref(ref: str, dest_ref: str) -> str:
    """Point `dest_ref` at the commit `ref` names. Return the commit hash."""
    commit = _git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    _git("update-ref", dest_ref, commit)
    return commit


def archive(commit: str) -> bytes:
    """The files of `commit` as a tar archive."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "agent.tar")
        _git("archive", "--output", str(path), commit)
        return path.read_bytes()


def bundle_all() -> bytes:
    """Every ref of the agent repo and the commits they reach, as a git bundle."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "all.bundle")
        _git("bundle", "create", "--quiet", str(path), "--all")
        return path.read_bytes()


def ref_commits() -> list[str]:
    """The commits the agent repo's refs point to."""
    return sorted(set(_git("for-each-ref", "--format=%(objectname)").split()))


def fetch_bundle(bundle: bytes, ref: str, dest_ref: str) -> tuple[str, str]:
    """Fetch `ref` from `bundle` into the agent repo as `dest_ref`. Return (commit, its parent)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "in.bundle")
        path.write_bytes(bundle)
        _git("fetch", "--quiet", str(path), f"{ref}:{dest_ref}")
    commit = _git("rev-parse", "--verify", f"{dest_ref}^{{commit}}").strip()
    return commit, _git("rev-parse", "--verify", f"{commit}^").strip()


def commit_message(commit: str) -> str:
    return _git("log", "-1", "--format=%B", commit).strip()


def commit_diff(commit: str) -> str:
    """Diff from the commit's parent."""
    return _git("diff", f"{commit}^", commit)
