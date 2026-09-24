"""The meta-agent's git checkout of the agent repo, inside a fresh sandbox each iteration.

A bundle of every ref in the agent repo goes in. A bundle of the new commit comes out, and the worker
fetches it into the agent repo.
"""

import shlex
from collections.abc import Generator
from contextlib import contextmanager

from e2b import CommandExitException, Sandbox, TimeoutException

from service import store
from service.config import OPTIMIZER_MODEL
from service.runner import SANDBOX_TEMPLATE

BASH_TIMEOUT_SEC = 120
SCRIPT_TIMEOUT_SEC = 300
# E2B deletes the sandbox after this, even if the worker died.
SANDBOX_TIMEOUT_SEC = 7200

SETUP_SCRIPT = """set -e
git init --quiet agent
cd agent
git config user.name {name}
git config user.email optimizer@localhost
git fetch --quiet --update-head-ok ../in.bundle 'refs/*:refs/*'
git checkout --quiet --detach {commit}
"""

# Prints "committed", or nothing when the files match HEAD.
COMMIT_SCRIPT = """set -e
git add --all -- . ':(exclude,glob)**/__pycache__/**'
git diff --cached --quiet && exit 0
git commit --quiet --no-verify --file ../message
git bundle create --quiet ../out.bundle HEAD
echo committed
"""


class E2BBox:
    """An E2B sandbox from SANDBOX_TEMPLATE. It gets no API keys and no database URL."""

    home = "/home/user"

    def __init__(self):
        self.sandbox = Sandbox.create(
            template=SANDBOX_TEMPLATE, timeout=SANDBOX_TIMEOUT_SEC, metadata={"service": "auto-harness", "role": "meta-agent"},
        )

    def run(self, command: str, cwd: str, timeout: int) -> tuple[int, str, str]:
        """Run `command` with bash. Return (exit code, stdout, stderr). Raise TimeoutError after `timeout` seconds."""
        try:
            result = self.sandbox.commands.run(command, cwd=cwd, timeout=timeout)
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
    """A git checkout of `commit` at `<box home>/agent`, with every ref of the agent repo."""

    def __init__(self, box: E2BBox, commit: str):
        self.box = box
        self.path = f"{box.home}/agent"
        box.write(f"{box.home}/in.bundle", store.bundle_all())
        self._script(SETUP_SCRIPT.format(name=shlex.quote(OPTIMIZER_MODEL), commit=commit), cwd=box.home)

    def bash(self, command: str) -> str:
        """Run `command` in the checkout. Return stdout, stderr, and the exit code."""
        try:
            code, stdout, stderr = self.box.run(command, cwd=self.path, timeout=BASH_TIMEOUT_SEC)
        except TimeoutError:
            return f"timed out after {BASH_TIMEOUT_SEC}s"
        # Postgres cannot store NUL in JSONB.
        return f"{stdout}{stderr}[exit code {code}]".replace("\x00", "")

    def commit(self, message: str, dest_ref: str) -> tuple[str, str] | None:
        """Commit the checkout's files on top of HEAD and fetch the commit into the agent repo as `dest_ref`.

        Return (commit, parent), or None when the files match HEAD.
        """
        self.box.write(f"{self.box.home}/message", message.encode())
        if not self._script(COMMIT_SCRIPT, cwd=self.path):
            return None
        return store.fetch_bundle(self.box.read(f"{self.box.home}/out.bundle"), dest_ref)

    def _script(self, script: str, cwd: str) -> str:
        """Run the service's own git commands. Unlike `bash`, a failure raises."""
        code, stdout, stderr = self.box.run(script, cwd=cwd, timeout=SCRIPT_TIMEOUT_SEC)
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
