import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text

# Tests truncate tables, so they use their own database. Set this before importing
# `service`, which reads its settings at import time.
TEST_DATABASE = "harness_test"
os.environ["SERVICE_DATABASE_URL"] = f"postgresql+psycopg://harness:harness@localhost:5432/{TEST_DATABASE}"
AGENT_REPO = Path(tempfile.mkdtemp(prefix="agent_repo_"))
os.environ["SERVICE_AGENT_REPO"] = str(AGENT_REPO)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from service.schemas import engine, init_db  # noqa: E402
from service.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def test_database():
    admin = create_engine("postgresql+psycopg://harness:harness@localhost:5432/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DATABASE}):
            conn.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    admin.dispose()
    init_db()


@pytest.fixture(autouse=True)
def clean_db(test_database):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE jobs, iterations, meta_messages"))


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class AgentRepo:
    """A fresh git repo at AGENT_REPO standing in for the operator's clone of the agent."""

    def __init__(self):
        self.path = AGENT_REPO
        shutil.rmtree(self.path)
        self.path.mkdir()
        self._git("init", "--quiet", "--initial-branch=main")
        self.commit_file("my_agent/core.py", "class Agent: ...\n")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit_file(self, path: str, content: str) -> str:
        file = self.path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self._git("add", path)
        self._git("commit", "--quiet", "-m", f"update {path}")
        return self._git("rev-parse", "HEAD")


@pytest.fixture
def agent_repo() -> AgentRepo:
    return AgentRepo()


class LocalBox:
    """Stands in for an E2B sandbox: a temp directory and a local shell with no inherited secrets."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="box_")

    def run(self, command, cwd, timeout, env=None):
        try:
            proc = subprocess.run(
                ["bash", "-c", command], cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout,
                env={"PATH": os.environ["PATH"], "HOME": self.home, **(env or {})},
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError from e
        return proc.returncode, proc.stdout, proc.stderr

    def write(self, path, data):
        Path(path).write_bytes(data)

    def read(self, path):
        return Path(path).read_bytes()

    def close(self):
        shutil.rmtree(self.home, ignore_errors=True)
