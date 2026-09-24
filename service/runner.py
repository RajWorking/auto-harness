import json
import os
from pathlib import Path

from e2b import CommandExitException, Sandbox, Template

from service import store
from service.config import AGENT_MODEL, DATASET
from service.schemas import AgentSource, TaskResult, TaskStatus

# Harbor enforces each task's own timeouts from its task.toml, then deletes the task's sandbox.
# This limit is a last resort: a killed Harbor process cannot delete its task sandboxes.
HARBOR_TIMEOUT_SEC = 3600
COMMAND_TIMEOUT_SEC = HARBOR_TIMEOUT_SEC + 300  # Harbor's limit plus time to write the report
# E2B deletes the outer sandbox after this, even if the worker died.
SANDBOX_TIMEOUT_SEC = HARBOR_TIMEOUT_SEC + 600
# The outer sandbox runs Harbor and the agent's Python code. Built from code at worker startup.
SANDBOX_TEMPLATE = "auto-harness-runner"
HARBOR = "harbor[e2b]==0.23.0"
# The only secrets the outer sandbox gets: Harbor creates task sandboxes, and the agent calls its model.
SANDBOX_SECRETS = ("E2B_API_KEY", "OPENAI_API_KEY")
RUN_DIR = "/home/user/run"
UPLOADS = [Path(__file__).parent.parent / "benchmark.py", Path(__file__).with_name("sandbox_run.py")]


def build_sandbox_template() -> None:
    """Build the outer sandbox's image. E2B caches unchanged steps, so a rebuild is fast."""
    template = Template().from_python_image("3.12").apt_install("git").pip_install(HARBOR)
    Template.build(template, SANDBOX_TEMPLATE, cpu_count=2, memory_mb=4096)


class HarborRunner:
    """Runs an agent commit on DATASET with `harbor run` inside 1 outer E2B sandbox.

    Harbor and the agent's Python code run in the outer sandbox. Each task runs in its own E2B sandbox,
    which Harbor creates from there. The worker host runs no agent code.
    """

    def run(self, agent: AgentSource, commit: str, task_ids: list[str]) -> list[TaskResult]:
        config = {
            "entrypoint": agent.entrypoint, "task_ids": task_ids, "dataset": DATASET,
            "agent_model": AGENT_MODEL, "harbor_timeout_sec": HARBOR_TIMEOUT_SEC,
        }
        report = _run_in_sandbox(store.archive(commit), config, commit)
        # The agent failed to load. Report that as its error on every task, so the optimizer sees it.
        if error := report["import_error"]:
            failure = f"agent failed to load:\n{error}"
            return [TaskResult(task_id=t, status=TaskStatus.error, reward=None, failure=failure) for t in task_ids]
        if not report["rewards"]:
            raise RuntimeError("harbor produced no job output; see the worker log")
        return [_task_result(t, report["rewards"].get(t), report["outputs"].get(t)) for t in task_ids]


def _run_in_sandbox(agent_tar: bytes, config: dict, commit: str) -> dict:
    sandbox = Sandbox.create(
        template=SANDBOX_TEMPLATE,
        timeout=SANDBOX_TIMEOUT_SEC,
        envs={k: os.environ[k] for k in SANDBOX_SECRETS if k in os.environ},
        metadata={"service": "auto-harness", "commit": commit},
    )
    try:
        sandbox.commands.run(f"mkdir -p {RUN_DIR}/agent")
        sandbox.files.write(f"{RUN_DIR}/agent.tar", agent_tar)
        for path in UPLOADS:
            sandbox.files.write(f"{RUN_DIR}/{path.name}", path.read_text())
        sandbox.files.write(f"{RUN_DIR}/run.json", json.dumps(config))
        try:
            result = sandbox.commands.run(
                "tar xf agent.tar -C agent && python sandbox_run.py", cwd=RUN_DIR,
                timeout=COMMAND_TIMEOUT_SEC, request_timeout=COMMAND_TIMEOUT_SEC,
            )
        except CommandExitException as e:
            raise RuntimeError(f"benchmark in the sandbox exited {e.exit_code}: {e.stderr[-2000:]}") from e
        print(result.stdout[-2000:], flush=True)  # Harbor's summary, for the worker log
        return json.loads(sandbox.files.read(f"{RUN_DIR}/report.json"))
    finally:
        sandbox.kill()


def _task_result(task_id: str, reward: float | None, output: str | None) -> TaskResult:
    # Same rules as benchmark.py: a reward of 0.5 or more passes, and no reward means the verifier did not run.
    if reward is None:
        return TaskResult(task_id=task_id, status=TaskStatus.error, reward=None, failure=output or "no verifier result")
    if reward >= 0.5:
        return TaskResult(task_id=task_id, status=TaskStatus.passed, reward=reward)
    return TaskResult(task_id=task_id, status=TaskStatus.failed, reward=reward, failure=output)
