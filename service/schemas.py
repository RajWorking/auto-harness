"""Database tables and API request/response shapes."""

import uuid
from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum

from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, String, create_engine, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from service.config import DATABASE_URL, META_DB_PASSWORD, META_DB_USER, TASK_IDS

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    """Whether the service finished the job. How the agent scored is in `result`."""

    queued = "queued"
    running = "running"
    completed = "completed"
    crashed = "crashed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[JobStatus]
    # The validated request: agent ref and entrypoint, task IDs, and loop settings.
    request: Mapped[dict] = mapped_column(JSONB)
    # Commit that `request.agent.ref` pointed to when the job was created.
    base_commit: Mapped[str] = mapped_column(String(40))
    # Why the optimization loop stopped.
    stop_reason: Mapped[str | None]
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Iteration(Base):
    __tablename__ = "iterations"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    index: Mapped[int] = mapped_column(primary_key=True)
    # Agent state at this step: a commit in the agent repo, kept by ref refs/jobs/<job_id>/<index>.
    # From iteration 1, the commit message holds the meta-agent's analysis and the diff holds its change.
    commit: Mapped[str] = mapped_column(String(40))
    # The commit the meta-agent built on. None for iteration 0.
    parent: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[dict] = mapped_column(JSONB)


class MetaMessage(Base):
    """The meta-agent's conversation, 1 row per message. Rows are only appended."""

    __tablename__ = "meta_messages"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    # The iteration the message belongs to. 0 for the system prompt.
    iteration: Mapped[int]
    role: Mapped[str]  # system, user, assistant, or tool
    # The message exactly as sent to or received from the model, tool calls and tool output included.
    message: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# The tables the meta-agent's `sql` tool can read.
META_AGENT_TABLES = ("jobs", "iterations")


def init_db() -> None:
    # Version 1 creates tables on startup. Schema changes require a fresh database.
    Base.metadata.create_all(engine)
    _grant_meta_agent()


def _grant_meta_agent() -> None:
    """Create the meta-agent's login role. It can read META_AGENT_TABLES and no other table."""
    role = sql.Identifier(META_DB_USER)
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        # The api and the worker both run this at startup. The lock keeps them from racing.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('grant_meta_agent'))")
        cur.execute(sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {}) THEN CREATE ROLE {}; END IF; END $$"
        ).format(sql.Literal(META_DB_USER), role))
        cur.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(META_DB_PASSWORD)))
        cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role))
        cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.SQL(", ").join(map(sql.Identifier, META_AGENT_TABLES)), role))
        conn.commit()
    finally:
        conn.close()


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


# API shapes


class AgentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Branch, tag, or commit in the agent repo. No leading '-', so git cannot read it as an option.
    ref: str = Field(pattern=r"^\w[\w./-]*$")
    # Harbor import path from the repository root, e.g. `agent.agent:HarnessAgent`.
    entrypoint: str = Field(pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: AgentSource
    task_ids: list[str] = Field(default_factory=lambda: list(TASK_IDS), min_length=1)
    # Optimization iterations after the first benchmark run. 0 runs the benchmark once.
    max_iterations: int = Field(0, ge=0, le=10)
    # The loop stops once an iteration's score (fraction of tasks passed) reaches this.
    target_score: float = Field(1.0, gt=0, le=1)

    @field_validator("task_ids")
    @classmethod
    def check_task_ids(cls, task_ids: list[str]) -> list[str]:
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task_ids contains duplicates")
        if unknown := sorted(set(task_ids) - set(TASK_IDS)):
            raise ValueError(f"unknown task_ids: {unknown}")
        return task_ids


class TaskStatus(StrEnum):
    passed = "passed"
    failed = "failed"
    error = "error"  # the verifier produced no reward


class TaskResult(BaseModel):
    task_id: str
    status: TaskStatus
    reward: float | None
    failure: str | None = None  # test or error output of a task that did not pass


class RunResult(BaseModel):
    score: float  # fraction of tasks passed
    tasks: list[TaskResult]


class JobOut(BaseModel):
    id: uuid.UUID
    status: JobStatus
    base_commit: str
    # The latest iteration: its commit and benchmark result. None until iteration 0 finishes.
    latest_commit: str | None
    latest_result: RunResult | None
    stop_reason: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class IterationOut(BaseModel):
    index: int
    commit: str
    analysis: str | None  # the meta-agent's reasoning for this change. None for iteration 0.
    parent: str | None  # the commit this iteration built on. None for iteration 0.
    diff: str | None  # the change from `parent`. None for iteration 0.
    result: RunResult


class MetaMessageOut(BaseModel):
    seq: int
    iteration: int
    role: str
    message: dict
    created_at: datetime
