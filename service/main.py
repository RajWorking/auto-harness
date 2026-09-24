import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from service import store
from service.schemas import (
    Iteration, IterationOut, Job, JobCreate, JobOut, JobStatus, MetaMessage, MetaMessageOut, get_session, init_db,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Agent Optimization Service", lifespan=lifespan)


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobOut)
def create_job(body: JobCreate, session: Session = Depends(get_session)) -> JobOut:
    """Queue a job. The worker runs it, and the caller polls `GET /jobs/{id}`."""
    job_id = uuid.uuid4()
    try:
        # Git first, then Postgres: a failed insert leaves only an unused ref behind.
        commit = store.pin_ref(body.agent.ref, store.iteration_ref(job_id, 0))
    except store.GitError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown ref: {e}")
    job = Job(id=job_id, status=JobStatus.queued, request=body.model_dump(), base_commit=commit)
    session.add(job)
    session.commit()
    session.refresh(job)  # load server-set timestamps
    return _job_out(session, job)


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> JobOut:
    return _job_out(session, _job_or_404(session, job_id))


@app.get("/jobs/{job_id}/iterations", response_model=list[IterationOut])
def list_iterations(job_id: uuid.UUID, session: Session = Depends(get_session)) -> list[IterationOut]:
    """Full history: each iteration's commit and parent, the meta-agent's analysis and diff, and its benchmark result."""
    _job_or_404(session, job_id)
    iterations = session.scalars(select(Iteration).where(Iteration.job_id == job_id).order_by(Iteration.index))
    return [
        IterationOut(
            index=it.index,
            commit=it.commit,
            analysis=store.commit_message(it.commit) if it.index else None,
            parent=it.parent,
            diff=store.commit_diff(it.commit) if it.index else None,
            result=it.result,
        )
        for it in iterations
    ]


@app.get("/jobs/{job_id}/transcript", response_model=list[MetaMessageOut])
def get_transcript(job_id: uuid.UUID, session: Session = Depends(get_session)) -> list[MetaMessage]:
    """The meta-agent's conversation: every message, tool call, and tool output, in order."""
    _job_or_404(session, job_id)
    return list(session.scalars(select(MetaMessage).where(MetaMessage.job_id == job_id).order_by(MetaMessage.seq)))


def _job_out(session: Session, job: Job) -> JobOut:
    latest = session.scalars(
        select(Iteration).where(Iteration.job_id == job.id).order_by(Iteration.index.desc()).limit(1)
    ).first()
    return JobOut(
        id=job.id,
        status=job.status,
        base_commit=job.base_commit,
        latest_commit=latest and latest.commit,
        latest_result=latest and latest.result,
        stop_reason=job.stop_reason,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _job_or_404(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job
