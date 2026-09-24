import subprocess
import sys
import uuid
from pathlib import Path

from service import store
from service.config import TASK_IDS
from service.schemas import SessionLocal, TaskResult, TaskStatus
from service.worker import claim_job, run_job
from service.tests.conftest import LocalBox

TASKS = TASK_IDS[:3]


def agent(ref="main"):
    return {"ref": ref, "entrypoint": "my_agent.core:Agent"}


class LineCountRunner:
    """Stands in for Harbor. Passes 1 more task for every `# improved` line in the agent file."""

    def run(self, agent, commit, task_ids):
        passed = store._git("show", f"{commit}:my_agent/core.py").count("# improved")
        return [
            TaskResult(task_id=t, status=TaskStatus.passed if i < passed else TaskStatus.failed, reward=float(i < passed))
            for i, t in enumerate(task_ids)
        ]


def scripted_meta_agent(*steps):
    """A stub meta-agent class. Each iteration runs the next step: `step(workdir)` edits the checkout's
    directory in the LocalBox and returns the analysis. `messages` records what the worker sent. Like
    MetaAgent, it records its conversation: 1 user and 1 assistant message per iteration."""

    class Scripted:
        instances = 0
        messages: list[str] = []

        def __init__(self, record):
            Scripted.instances += 1
            self.record = record

        def run(self, message, work):
            Scripted.messages.append(message)
            self.record({"role": "user", "content": message})
            analysis = steps[len(Scripted.messages) - 1](Path(work.path))
            self.record({"role": "assistant", "content": analysis})
            return analysis

    return Scripted


def append(line, analysis):
    def step(workdir):
        with open(workdir / "my_agent/core.py", "a") as f:
            f.write(line)
        return analysis

    return step


def run_worker_once(runner=None, meta_agent=None):
    with SessionLocal() as session:
        job = claim_job(session)
        assert job is not None
        run_job(session, job, runner or LineCountRunner(), meta_agent or _no_meta_agent, LocalBox)


def _no_meta_agent(record):
    raise AssertionError("meta-agent created")


def submit(client, **body):
    resp = client.post("/jobs", json={"agent": agent(), **body})
    assert resp.status_code == 202, resp.json()
    return resp.json()["id"]


def test_post_queues_job(client, agent_repo):
    job_id = submit(client)
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"


def test_worker_runs_all_tasks_by_default(client, agent_repo):
    job_id = submit(client)
    run_worker_once()

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["stop_reason"] == "max_iterations"
    assert job["latest_commit"] == job["base_commit"]
    tasks = job["latest_result"]["tasks"]
    assert [t["task_id"] for t in tasks] == TASK_IDS
    assert job["latest_result"]["score"] == sum(t["status"] == "passed" for t in tasks) / len(tasks)

    [iteration] = client.get(f"/jobs/{job_id}/iterations").json()
    assert iteration["index"] == 0 and iteration["parent"] is None and iteration["analysis"] is None


def test_job_pins_ref_to_commit_in_store(client, agent_repo):
    first_commit = agent_repo.commit_file("my_agent/core.py", "class Agent: 'v1'\n")
    job_id = submit(client)
    agent_repo.commit_file("my_agent/core.py", "class Agent: 'v2'\n")  # branch moves after submission

    assert client.get(f"/jobs/{job_id}").json()["base_commit"] == first_commit
    ref = store.iteration_ref(job_id, 0)
    assert store._git("rev-parse", ref).strip() == first_commit


def test_ref_can_be_a_commit_hash(client, agent_repo):
    commit = agent_repo.commit_file("my_agent/core.py", "class Agent: 'pinned'\n")
    resp = client.post("/jobs", json={"agent": agent(ref=commit)})
    assert resp.json()["base_commit"] == commit


def test_unknown_job_is_404(client):
    assert client.get(f"/jobs/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/jobs/{uuid.uuid4()}/iterations").status_code == 404
    assert client.get(f"/jobs/{uuid.uuid4()}/transcript").status_code == 404


def test_rejects_invalid_requests(client, agent_repo):
    bad_bodies = [
        {},
        {"agent": agent(), "task_ids": ["not-a-task"]},
        {"agent": agent(), "task_ids": []},
        {"agent": agent(), "task_ids": [TASK_IDS[0], TASK_IDS[0]]},
        {"agent": agent(), "max_iterations": -1},
        {"agent": agent(), "max_iterations": 11},
        {"agent": agent(), "target_score": 0},
        {"agent": agent(), "target_score": 1.5},
        {"agent": {**agent(), "entrypoint": "not an import path"}},
        {"agent": agent(ref="main:refs/heads/other")},
        {"agent": agent(ref="--upload-pack=x")},
        {"agent": agent(), "unexpected": 1},
    ]
    for body in bad_bodies:
        assert client.post("/jobs", json=body).status_code == 422, body


def test_rejects_unknown_ref(client, agent_repo):
    resp = client.post("/jobs", json={"agent": agent(ref="no-such-branch")})
    assert resp.status_code == 422
    assert "unknown ref" in resp.json()["detail"]


def test_runner_crash_marks_job_crashed(client, agent_repo):
    class CrashingRunner:
        def run(self, agent, commit, task_ids):
            raise RuntimeError("sandbox died")

    job_id = submit(client)
    run_worker_once(CrashingRunner())
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "crashed"
    assert job["error"] == "RuntimeError: sandbox died"
    assert job["latest_result"] is None


def test_task_errors_do_not_crash_job(client, agent_repo):
    class AllErrorRunner:
        def run(self, agent, commit, task_ids):
            return [TaskResult(task_id=t, status=TaskStatus.error, reward=None) for t in task_ids]

    job_id = submit(client)
    run_worker_once(AllErrorRunner())
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["latest_result"]["score"] == 0


def test_loop_stops_at_target_score(client, agent_repo):
    def improve_and_check_history(workdir):
        # The checkout shares the repo's refs, so the meta-agent sees every iteration's commit.
        refs = subprocess.run(["git", "for-each-ref", "refs/jobs/"], cwd=workdir, capture_output=True, text=True).stdout
        assert f"refs/jobs/{job_id}/0" in refs
        return append("# improved\n", "attempt 2")(workdir)

    job_id = submit(client, task_ids=TASKS, max_iterations=5, target_score=0.6)
    meta = scripted_meta_agent(append("# improved\n", "attempt 1"), improve_and_check_history)
    run_worker_once(LineCountRunner(), meta)

    job = client.get(f"/jobs/{job_id}").json()
    iterations = client.get(f"/jobs/{job_id}/iterations").json()
    assert job["status"] == "completed"
    assert job["stop_reason"] == "target_score"
    assert [it["result"]["score"] for it in iterations] == [0, 1 / 3, 2 / 3]
    assert job["latest_commit"] == iterations[-1]["commit"]
    assert job["latest_result"]["score"] == 2 / 3
    assert iterations[2]["analysis"] == "attempt 2"
    assert "+# improved" in iterations[2]["diff"]
    # By default, each iteration builds on the previous one.
    assert [it["parent"] for it in iterations] == [None, iterations[0]["commit"], iterations[1]["commit"]]
    # 1 meta-agent for the job. Each message reports the previous iteration's outcome.
    assert meta.instances == 1
    assert f"Iteration 0, commit {job['base_commit']}, scored 0.00" in meta.messages[0]
    assert f"Iteration 1, commit {iterations[1]['commit']} on parent {job['base_commit']}, scored 0.33." in meta.messages[1]
    # The transcript holds every message in order, tagged with its iteration.
    transcript = client.get(f"/jobs/{job_id}/transcript").json()
    assert [(m["seq"], m["iteration"], m["role"]) for m in transcript] == [
        (0, 1, "user"), (1, 1, "assistant"), (2, 2, "user"), (3, 2, "assistant"),
    ]
    assert transcript[3]["message"]["content"] == "attempt 2"


def test_every_change_is_committed_and_meta_agent_picks_the_parent(client, agent_repo):
    def checkout(commit_of):
        def step(workdir):
            subprocess.run(["git", "checkout", "--quiet", "--detach", commit_of()], cwd=workdir, check=True)
            return append("# improved\n", "build on the chosen commit")(workdir)

        return step

    job_id = submit(client, task_ids=TASKS, max_iterations=3)
    iterations = lambda: client.get(f"/jobs/{job_id}/iterations").json()  # noqa: E731
    meta = scripted_meta_agent(
        append("# comment\n", "a change that does not help"),
        checkout(lambda: iterations()[0]["commit"]),  # go back to the base commit
        append("# improved\n", "keep building"),
    )
    run_worker_once(LineCountRunner(), meta)

    its = iterations()
    assert [it["result"]["score"] for it in its] == [0, 0, 1 / 3, 2 / 3]
    assert [it["parent"] for it in its] == [None, its[0]["commit"], its[0]["commit"], its[2]["commit"]]
    # Iteration 1 is committed though it did not help. Iteration 2 left it out by building on the base.
    assert "# comment" not in store._git("show", f"{its[3]['commit']}:my_agent/core.py")
    assert f"the checkout is at commit {its[1]['commit']}" in meta.messages[1]
    assert client.get(f"/jobs/{job_id}").json()["stop_reason"] == "max_iterations"


def test_meta_agent_can_change_several_files(client, agent_repo):
    def add_module(workdir):
        (workdir / "my_agent/tools.py").write_text("TOOLS = []\n")
        return append("# improved\n", "split out tools")(workdir)

    job_id = submit(client, task_ids=TASKS, max_iterations=1)
    run_worker_once(LineCountRunner(), scripted_meta_agent(add_module))

    [_, iteration] = client.get(f"/jobs/{job_id}/iterations").json()
    assert "my_agent/tools.py" in iteration["diff"]
    assert "my_agent/core.py" in iteration["diff"]


def test_pycache_is_not_committed(client, agent_repo):
    def edit_and_compile(workdir):
        analysis = append("# improved\n", "compiled")(workdir)
        subprocess.run([sys.executable, "-m", "py_compile", "my_agent/core.py"], cwd=workdir, check=True)
        assert list(workdir.glob("my_agent/__pycache__/*.pyc"))
        return analysis

    job_id = submit(client, task_ids=TASKS, max_iterations=1)
    run_worker_once(LineCountRunner(), scripted_meta_agent(edit_and_compile))

    [_, iteration] = client.get(f"/jobs/{job_id}/iterations").json()
    assert store._git("ls-tree", "-r", "--name-only", iteration["commit"]).split() == ["my_agent/core.py"]


def test_failed_or_empty_attempt_uses_up_iteration(client, agent_repo):
    job_id = submit(client, task_ids=TASKS, max_iterations=3)
    def edit_then_fail(workdir):
        append("# partial\n", "")(workdir)
        raise RuntimeError("no final reply")

    meta = scripted_meta_agent(edit_then_fail, lambda workdir: "no change", append("# improved\n", "fix"))
    run_worker_once(LineCountRunner(), meta)

    job = client.get(f"/jobs/{job_id}").json()
    iterations = client.get(f"/jobs/{job_id}/iterations").json()
    assert job["status"] == "completed"
    assert [it["index"] for it in iterations] == [0, 3]
    assert iterations[1]["parent"] == job["base_commit"]
    assert "# partial" not in iterations[1]["diff"]  # the failed attempt's edit was dropped
    assert "Iteration 1 failed and was not benchmarked: RuntimeError: no final reply" in meta.messages[1]
    assert "Iteration 2 changed no files" in meta.messages[2]


def test_claim_takes_oldest_queued_job_once(client, agent_repo):
    first, second = submit(client), submit(client)
    with SessionLocal() as session:
        assert str(claim_job(session).id) == first
        assert str(claim_job(session).id) == second
        assert claim_job(session) is None


def test_crashed_job_has_no_stop_reason(client, agent_repo):
    class CrashOnSecondRun:
        calls = 0

        def run(self, agent, commit, task_ids):
            CrashOnSecondRun.calls += 1
            if CrashOnSecondRun.calls == 2:
                raise RuntimeError("sandbox died")
            return LineCountRunner().run(agent, commit, task_ids)

    job_id = submit(client, task_ids=TASKS, max_iterations=3)
    run_worker_once(CrashOnSecondRun(), scripted_meta_agent(append("# x\n", "change")))
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "crashed"
    assert job["stop_reason"] is None
    assert job["latest_commit"] == job["base_commit"]  # iteration 0 is kept
    assert len(client.get(f"/jobs/{job_id}/iterations").json()) == 1


def test_failed_transcript_write_does_not_break_the_job(client, agent_repo):
    class RecordsNul:
        def __init__(self, record):
            self.record = record

        def run(self, message, work):
            self.record({"role": "tool", "content": "a\x00b"})  # Postgres rejects NUL in JSONB
            self.record({"role": "assistant", "content": "fix"})
            return append("# improved\n", "fix")(Path(work.path))

    job_id = submit(client, task_ids=TASKS, max_iterations=1)
    run_worker_once(LineCountRunner(), RecordsNul)

    assert client.get(f"/jobs/{job_id}").json()["status"] == "completed"
    assert [m["seq"] for m in client.get(f"/jobs/{job_id}/transcript").json()] == [1]
