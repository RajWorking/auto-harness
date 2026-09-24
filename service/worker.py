"""Claims queued jobs and runs the optimization loop on each.

Start 1 worker: on startup it marks every running job as crashed.
"""

import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from service import checkout, optimizer, store
from service.schemas import (
    AgentSource, Iteration, Job, JobCreate, JobStatus, MetaMessage, RunResult, SessionLocal, TaskStatus, init_db,
)
from service.runner import HarborRunner, build_sandbox_template

POLL_SEC = 2


def claim_job(session: Session) -> Job | None:
    """Move the oldest queued job to running. SKIP LOCKED gives each queued job to exactly 1 claimer."""
    job = session.scalars(
        select(Job).where(Job.status == JobStatus.queued).order_by(Job.created_at).limit(1).with_for_update(skip_locked=True)
    ).first()
    if job is not None:
        job.status = JobStatus.running
        session.commit()
    return job


def run_job(
    session: Session, job: Job, runner: HarborRunner, meta_agent=optimizer.MetaAgent, box_factory=checkout.E2BBox,
) -> None:
    """Benchmark the agent, then let the meta-agent change it until an iteration's score reaches
    `target_score` or `max_iterations` iterations have run.

    The job crashes only when the service cannot produce a result. Failed tasks are part of a completed job.
    """
    try:
        request = JobCreate.model_validate(job.request)
        latest = _benchmark(runner, request.agent, job.base_commit, request.task_ids)
        _save_iteration(session, job, 0, job.base_commit, None, latest)
        if request.max_iterations and latest.score < request.target_score:
            latest = _optimize(session, job, request, runner, meta_agent, box_factory, latest)
        job.stop_reason = "target_score" if latest.score >= request.target_score else "max_iterations"
        job.status = JobStatus.completed
    except Exception as e:
        session.rollback()
        job.status = JobStatus.crashed
        job.error = f"{type(e).__name__}: {e}"
    session.commit()


def _optimize(
    session: Session, job: Job, request: JobCreate, runner: HarborRunner, meta_agent, box_factory, latest: RunResult,
) -> RunResult:
    """Run the improvement iterations. Return the latest benchmark result.

    Each iteration gives the meta-agent a fresh sandbox with a checkout of the agent repo. The meta-agent
    picks the commit to build on by checking it out. Every change is committed on top of it and benchmarked.
    """
    transcript = _TranscriptWriter(job.id)
    meta = meta_agent(transcript)
    head = job.base_commit
    outcome = (
        f"Job {job.id}. Entrypoint `{request.agent.entrypoint}`. Tasks: {', '.join(request.task_ids)}.\n"
        f"Iteration 0, commit {job.base_commit}, scored {latest.score:.2f}. Target score: {request.target_score}."
    )
    for index in range(1, request.max_iterations + 1):
        transcript.iteration = index
        message = (
            f"{outcome}\nIteration {index} of {request.max_iterations}: the checkout is at commit "
            f"{head}. Check out the commit you want to build on, then make your change."
        )
        with checkout.open_checkout(head, box_factory) as work:
            try:
                analysis = meta.run(message, work)
                committed = work.commit(analysis, store.iteration_ref(job.id, index))
            except Exception as e:
                # A failed attempt uses up the iteration. It has no commit, so it has no iterations row.
                outcome = f"Iteration {index} failed and was not benchmarked: {type(e).__name__}: {e}"
                continue
        if committed is None:
            outcome = f"Iteration {index} changed no files, so it was not benchmarked."
            continue
        head, parent = committed
        latest = _benchmark(runner, request.agent, head, request.task_ids)
        _save_iteration(session, job, index, head, parent, latest)
        outcome = f"Iteration {index}, commit {head} on parent {parent}, scored {latest.score:.2f}."
        if latest.score >= request.target_score:
            break
    return latest


class _TranscriptWriter:
    """Appends each meta-agent message to `meta_messages`. A failed write loses only that row."""

    def __init__(self, job_id):
        self.job_id = job_id
        self.iteration = 0  # the system prompt
        self.seq = 0

    def __call__(self, message: dict) -> None:
        row = MetaMessage(job_id=self.job_id, seq=self.seq, iteration=self.iteration, role=message["role"], message=message)
        self.seq += 1
        try:
            with SessionLocal() as session:
                session.add(row)
                session.commit()
        except Exception as e:
            print(f"job {self.job_id}: transcript row {row.seq} not stored: {type(e).__name__}: {e}", flush=True)


def _benchmark(runner: HarborRunner, agent: AgentSource, commit: str, task_ids: list[str]) -> RunResult:
    tasks = runner.run(agent, commit, task_ids)
    return RunResult(score=sum(t.status == TaskStatus.passed for t in tasks) / len(tasks), tasks=tasks)


def _save_iteration(session: Session, job: Job, index: int, commit: str, parent: str | None, result: RunResult) -> None:
    session.add(Iteration(job_id=job.id, index=index, commit=commit, parent=parent, result=result.model_dump(mode="json")))
    session.commit()


def main() -> None:
    init_db()
    build_sandbox_template()
    runner = HarborRunner()
    with SessionLocal() as session:
        # A running job at startup was interrupted by a worker restart.
        session.execute(
            update(Job).where(Job.status == JobStatus.running).values(status=JobStatus.crashed, error="worker restarted")
        )
        session.commit()
    print("worker started", flush=True)
    while True:
        with SessionLocal() as session:
            job = claim_job(session)
            if job is None:
                time.sleep(POLL_SEC)
                continue
            print(f"job {job.id}: running", flush=True)
            run_job(session, job, runner)
            print(f"job {job.id}: {job.status}", flush=True)


if __name__ == "__main__":
    main()
