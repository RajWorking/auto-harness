import subprocess
import sys
import tempfile
from pathlib import Path

from benchmark import TerminalBenchRunner
from service import store
from service.config import AGENT_MODEL, DATASET, ENV_PROVIDER
from service.schemas import AgentSource, TaskResult, TaskStatus

OUTPUT_CHARS = 3000  # tail of each failed task's output kept for the optimizer
# Harbor enforces each task's own timeouts from its task.toml, then deletes the task's sandbox.
# This limit is a last resort: a killed Harbor process cannot delete its E2B sandboxes.
RUN_TIMEOUT_SEC = 3600


class HarborRunner:
    """Runs an agent commit on DATASET with `harbor run`. Each task runs in its own ENV_PROVIDER sandbox."""

    def run(self, agent: AgentSource, commit: str, task_ids: list[str]) -> list[TaskResult]:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir, jobs_dir = Path(tmp, "agent"), Path(tmp, "jobs")
            store.export(commit, agent_dir)
            # Harbor exits without output when the agent fails to import. Report that as the
            # agent's error on every task, so the optimizer sees it and the job does not crash.
            if error := _import_error(agent_dir, agent.entrypoint):
                failure = f"agent failed to load:\n{error}"
                return [TaskResult(task_id=t, status=TaskStatus.error, reward=None, failure=failure) for t in task_ids]
            rewards = TerminalBenchRunner(
                agent_model=AGENT_MODEL,
                split=None,
                dataset=DATASET,
                env_provider=ENV_PROVIDER,
                n_concurrent=len(task_ids),
                per_task_timeout=180,  # agent timeout multiplier 1.0: use each task's own timeout
                agent_import_path=agent.entrypoint,
                jobs_dir=str(jobs_dir),
                agent_dir=str(agent_dir),
                subprocess_timeout=RUN_TIMEOUT_SEC,
            ).run(task_ids)
            if not rewards:
                raise RuntimeError("harbor produced no job output; see the worker log")
            outputs = {
                trial.name.rsplit("__", 1)[0]: _output(trial)
                for job_dir in jobs_dir.iterdir() if job_dir.is_dir()
                for trial in job_dir.iterdir() if trial.is_dir()
            }
        return [_task_result(t, rewards.get(t), outputs.get(t)) for t in task_ids]


def _import_error(agent_dir: Path, entrypoint: str) -> str | None:
    """Import the entrypoint from `agent_dir` as Harbor would. Return the error output, or None."""
    module, name = entrypoint.split(":")
    proc = subprocess.run(
        [sys.executable, "-c", f"import importlib; getattr(importlib.import_module({module!r}), {name!r})"],
        cwd=agent_dir, capture_output=True, text=True, timeout=120,
    )
    return proc.stderr[-OUTPUT_CHARS:] if proc.returncode else None


def _output(trial: Path) -> str | None:
    for name in ("verifier/test-stdout.txt", "exception.txt"):
        if (file := trial / name).exists():
            # Postgres cannot store NUL in JSONB.
            return file.read_text(errors="replace").replace("\x00", "")[-OUTPUT_CHARS:]
    return None


def _task_result(task_id: str, reward: float | None, output: str | None) -> TaskResult:
    # Same rules as benchmark.py: a reward of 0.5 or more passes, and no reward means the verifier did not run.
    if reward is None:
        return TaskResult(task_id=task_id, status=TaskStatus.error, reward=None, failure=output or "no verifier result")
    if reward >= 0.5:
        return TaskResult(task_id=task_id, status=TaskStatus.passed, reward=reward)
    return TaskResult(task_id=task_id, status=TaskStatus.failed, reward=reward, failure=output)

