"""The meta-agent's git checkout of the agent repo, inside a sandbox. Each iteration gets a fresh one.

Git moves commits in both directions. A bundle of every ref in the agent repo goes in. A bundle of the
new commit comes out, and the worker fetches it into the agent repo.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

from e2b import CommandExitException, Sandbox, TimeoutException

from service import store
from service.runner import SANDBOX_TEMPLATE

BASH_TIMEOUT_SEC = 120
# E2B deletes the sandbox after this, even if the worker died. 1 iteration of the meta-agent fits well within it.
SANDBOX_TIMEOUT_SEC = 7200
SETUP_TIMEOUT_SEC = 300

# Runs in the checkout. Prints "<commit> <parent>", or nothing when the files match HEAD.
COMMIT_SCRIPT = """set -e
git add --all -- . ':(exclude,glob)**/__pycache__/**'
tree=$(git write-tree)
parent=$(git rev-parse HEAD)
[ "$tree" = "$(git rev-parse "$parent^{tree}")" ] && exit 0
commit=$(git commit-tree "$tree" -p "$parent" -F ../message)
git update-ref refs/out "$commit"
(echo refs/out; cat ../known) | git bundle create --quiet ../out.bundle --stdin
echo "$commit $parent"
"""


class Box(Protocol):
    """A sandbox: its home directory, a shell, and its files."""

    home: str

    def run(self, command: str, cwd: str, timeout: int, env: dict | None = None) -> tuple[int, str, str]:
        """Run `command` with bash. Return (exit code, stdout, stderr). Raise TimeoutError after `timeout` seconds."""

    def write(self, path: str, data: bytes) -> None: ...

    def read(self, path: str) -> bytes: ...

    def close(self) -> None: ...


class E2BBox:
    """An E2B sandbox from SANDBOX_TEMPLATE. It gets no API keys and no database URL."""

    home = "/home/user"

    def __init__(self):
        self.sandbox = Sandbox.create(
            template=SANDBOX_TEMPLATE, timeout=SANDBOX_TIMEOUT_SEC, metadata={"service": "auto-harness", "role": "meta-agent"},
        )

    def run(self, command: str, cwd: str, timeout: int, env: dict | None = None) -> tuple[int, str, str]:
        try:
            result = self.sandbox.commands.run(command, cwd=cwd, envs=env, timeout=timeout)
        except CommandExitException as e:
            return e.exit_code, e.stdout, e.stderr
        except TimeoutException as e:
            raise TimeoutError from e
        return result.exit_code, result.stdout, result.stderr

    def write(self, path: str, data: bytes) -> None:
        self.sandbox.files.write(path, data)

    def read(self, path: str) -> bytes:
        return bytes(self.sandbox.files.read(path, format="bytes"))

    def close(self) -> None:
        self.sandbox.kill()


class Checkout:
    """A git checkout of the agent repo at `<box home>/agent`, with every ref of the agent repo."""

    def __init__(self, box: Box, commit: str):
        self.box = box
        self.path = f"{box.home}/agent"
        # Commits the agent repo already has. The bundle that comes out leaves them out.
        self.known = store.ref_commits()
        box.write(f"{box.home}/in.bundle", store.bundle_all())
        self._script(
            f"git init --quiet {self.path} && cd {self.path}"
            " && git config user.name optimizer && git config user.email optimizer@localhost"
            " && git fetch --quiet --update-head-ok ../in.bundle 'refs/*:refs/*'"
            f" && git checkout --quiet --detach {commit}",
            cwd=box.home,
        )

    def bash(self, command: str) -> str:
        """Run `command` in the checkout. Return stdout, stderr, and the exit code."""
        try:
            code, stdout, stderr = self.box.run(command, cwd=self.path, timeout=BASH_TIMEOUT_SEC)
        except TimeoutError:
            return f"timed out after {BASH_TIMEOUT_SEC}s"
        # Postgres cannot store NUL in JSONB, and every tool output goes to meta_messages.
        return f"{stdout}{stderr}[exit code {code}]".replace("\x00", "")

    def head(self) -> str:
        return self._script("git rev-parse HEAD").strip()

    def commit(self, message: str, dest_ref: str) -> tuple[str, str] | None:
        """Commit the checkout's files on top of its HEAD and fetch the commit into the agent repo as `dest_ref`.

        Return (new commit, parent), or None when the files match HEAD. `__pycache__` directories are never committed.
        """
        self.box.write(f"{self.box.home}/message", message.encode())
        self.box.write(f"{self.box.home}/known", "".join(f"^{c}\n" for c in self.known).encode())
        if not self._script(COMMIT_SCRIPT, env=store.OPTIMIZER_IDENTITY).strip():
            return None
        return store.fetch_bundle(self.box.read(f"{self.box.home}/out.bundle"), "refs/out", dest_ref)

    def _script(self, script: str, cwd: str | None = None, env: dict | None = None) -> str:
        """Run the service's own git commands. Unlike `bash`, a failure raises."""
        code, stdout, stderr = self.box.run(script, cwd=cwd or self.path, timeout=SETUP_TIMEOUT_SEC, env=env)
        if code:
            raise store.GitError(f"exit code {code}: {stderr.strip()[-2000:]}")
        return stdout


@contextmanager
def open_checkout(commit: str, box_factory=E2BBox) -> Generator[Checkout]:
    """A fresh sandbox with a checkout of `commit`. The sandbox is deleted on exit."""
    box = box_factory()
    try:
        yield Checkout(box, commit)
    finally:
        box.close()
