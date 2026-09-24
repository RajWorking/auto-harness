"""Runs inside the outer E2B sandbox, next to benchmark.py and the agent's files.

Only this file, benchmark.py, and the agent are uploaded, so it imports nothing from `service`.
It reads run.json and writes report.json.
"""

import json
import subprocess
import sys
from pathlib import Path

OUTPUT_CHARS = 3000  # tail of each task's output kept for the optimizer


def main() -> None:
    config = json.loads(Path("run.json").read_text())
    agent_dir, jobs_dir = Path("agent").resolve(), Path("jobs").resolve()
    report = {"import_error": _import_error(agent_dir, config["entrypoint"]), "rewards": {}, "outputs": {}}
    # Harbor exits without output when the agent fails to import, so skip the run.
    if report["import_error"] is None:
        from benchmark import TerminalBenchRunner

        report["rewards"] = TerminalBenchRunner(
            agent_model=config["agent_model"],
            split=None,
            dataset=config["dataset"],
            env_provider="e2b",  # the outer sandbox has E2B credentials and no Docker
            n_concurrent=len(config["task_ids"]),
            per_task_timeout=180,  # agent timeout multiplier 1.0: use each task's own timeout
            agent_import_path=config["entrypoint"],
            jobs_dir=str(jobs_dir),
            agent_dir=str(agent_dir),
            subprocess_timeout=config["harbor_timeout_sec"],
        ).run(config["task_ids"])
        report["outputs"] = {
            trial.name.rsplit("__", 1)[0]: _output(trial)
            for job_dir in jobs_dir.iterdir() if job_dir.is_dir()
            for trial in job_dir.iterdir() if trial.is_dir()
        }
    Path("report.json").write_text(json.dumps(report))


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


if __name__ == "__main__":
    main()
