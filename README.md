# auto-harness

> Give a coding agent a benchmark and an agent file. Let it iterate overnight. It reads failures, improves the system prompt and tools, gates every change against a self-maintained eval suite, and repeats.

This repo is a simplified version of our auto-harness agent setup. We demonstrate our system on Tau3 benchmark tasks where the agent's score improves from 0.56 to 0.78 (~40% jump) while mining failures and auto maintaining live evals. If you are curious to learn more, read the full blog here - https://www.neosigma.ai/blog/self-improving-agentic-systems.

The loop is defined in `PROGRAM.md`. The coding agent edits `agent/agent.py` to improve the agent and appends findings to `workspace/learnings.md` after each iteration.

---

## Supported Benchmarks

| Benchmark | Domain | Tasks | Agent Interface |
|-----------|--------|-------|-----------------|
| **tau-bench** | Customer service (retail, airline, telecom) | retail: 114, airline: 50, telecom: 114 | Structured tool calls via tau2 |
| **Terminal-Bench 2.0** | Real-world terminal tasks (coding, sysadmin, security) | 89 | Bash commands via Harbor containers |
| **BIRD-Interact** | Interactive text-to-SQL (multi-turn, CRUD over Postgres) | lite: 300, full: 600 | Google ADK agent against a 3-service environment (user sim, DB env, system agent) |

---

## How it works

```
run benchmark → analyze → improve agent/agent.py → gate → record → update learnings → repeat
```

- **`agent/agent.py`** — the agent being optimized (copied from a benchmark-specific template)
- **`agent/templates/`** — starting-point templates for each benchmark (read-only)
- **`benchmark.py`** — runs your benchmark, returns per-task rewards
- **`gating.py`** — three-step gate: eval suite + full test val_score + suite promotion
- **`record.py`** — appends iteration results to `workspace/results.tsv`
- **`prepare.py`** — sets up workspace, copies templates, runs baseline
- **`program_templates/`** — benchmark-specific PROGRAM.md instructions
- **`PROGRAM.md`** — instructions the coding agent follows (copied from template by prepare.py)

---

## First-run checklist

Before running the initialization command in your chosen quick start:

- [ ] **Choose one benchmark and install its prerequisites:** [Terminal-Bench 2.0](#quick-start-terminal-bench-20) needs the `harbor` CLI and a configured sandbox provider (or a running Docker daemon for `env_provider: "docker"`); [BIRD-Interact](#quick-start-bird-interact) needs Python 3.12+, Docker, `git-lfs`, and access to the ground-truth data described below; [tau-bench](#quick-start-tau-bench) uses Docker Compose and the image built by `docker compose build autoeval`.
- [ ] **Configure the experiment:** copy [`experiment_config.yaml.template`](experiment_config.yaml.template) to `experiment_config.yaml`, uncomment only the benchmark section you intend to use, and review its models, dataset/domain, and `max_concurrency`. The template is entirely commented out, so copying it alone does not select a benchmark.
- [ ] **Set credentials for the selected models and provider:** use [`.env.example`](.env.example) as a reference. For Terminal-Bench, supply the credential for the selected `env_provider`; Docker needs no sandbox-provider key, but model API credentials are still required. For BIRD-Interact, check both `agent_model` and `user_model`.
- [ ] **Make credentials available to the process:** Docker Compose loads `.env` via `env_file`. When running `python prepare.py` directly, export the required variables in your shell first; `prepare.py` does not load `.env` automatically. For a shell-compatible `.env` you have reviewed, Bash/Zsh users can run `set -a; source .env; set +a` from the repository root.
- [ ] **Plan for a baseline run:** on a fresh workspace, `python prepare.py` (or `docker compose run autoeval python prepare.py`) performs setup and launches the baseline benchmark. This makes model API calls and may incur sandbox-provider charges. Review your provider budget before running it; it is not a setup-only check.

After initialization completes, follow [Running the loop](#running-the-loop). Optimization and [gating](#eval-suite) run additional benchmark tasks and can incur further usage charges.

---

## Quick start: Terminal-Bench 2.0

**Requirements:** `harbor` CLI, an `OPENAI_API_KEY`, and a coding agent (Claude Code, Codex CLI, or similar). If using a sandboxed `env_provider` (the default), you'll also need its credential: `E2B_API_KEY`, `DAYTONA_API_KEY`, or a Modal token via `modal token new` / `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET`. `env_provider: "docker"` needs none of these.

```bash
# 1. Clone the repo
git clone https://github.com/neosigmaai/auto-harness
cd auto-harness

# 2. Install harbor
uv tool install harbor

# 3. Set up environment variables
cp .env.example .env
# edit .env — set OPENAI_API_KEY, plus your sandbox provider's credential
# (E2B_API_KEY, DAYTONA_API_KEY, or MODAL_TOKEN_ID + MODAL_TOKEN_SECRET) —
# not needed if you're using env_provider: "docker"

# 4. Configure the experiment
cp experiment_config.yaml.template experiment_config.yaml
# edit experiment_config.yaml — uncomment the terminal-bench section

# 5. Initialize workspace + run baseline (runs all 89 tasks, generates train/test split)
python prepare.py

# 6. Start the optimization loop
# Point your coding agent at the repo and prompt:
#   "Read PROGRAM.md and start the optimization loop."
```

## Quick start: BIRD-Interact

**Requirements:** Docker (for Postgres), Python 3.12+, `git-lfs` (for the HF dataset), an `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` depending on model), and a coding agent.

```bash
# 1. Clone this repo
git clone https://github.com/neosigmaai/auto-harness
cd auto-harness

# 2. Set up environment variables
cp .env.example .env
# edit .env — set OPENAI_API_KEY (or ANTHROPIC_API_KEY)

# 3. Configure the experiment
cp experiment_config.yaml.template experiment_config.yaml
# edit experiment_config.yaml — uncomment the BIRD-INTERACT section

# 4. Initialize — prepare.py auto-provisions everything:
#      - clones BIRD-Interact-ADK into ./bird_interact_adk/ (gitignored)
#      - creates an isolated .venv-adk with the ADK's deps
#      - clones the bird-interact-lite dataset from HuggingFace
#      - starts the Postgres Docker container
#      - runs the baseline (300 tasks) and generates the train/test split
python prepare.py

# 5. Start the optimization loop
# Point your coding agent at the repo and prompt:
#   "Read PROGRAM.md and start the optimization loop."
```

**Ground truth (one-time step):** The public BIRD-Interact dataset ships *without* gold SQL to prevent data leakage. On first run, `prepare.py` will detect this and print the exact email + merge command needed. Briefly:

1. Email `bird.bench25@gmail.com` with subject `[bird-interact-lite GT&Test Cases]`
2. Run the `combine_public_with_gt.py` script shown by prepare.py, using the jsonl you receive
3. Re-run `python prepare.py`

**What the integration adds:**

- `BirdInteractRunner` in `benchmark.py` — spawns the three ADK services (user simulator, DB environment, system agent) per run, drives `orchestrator.runner`, parses results into the harness reward format.
- `agent/helpers/bird_interact/bird_service.py` + `agent/helpers/bird_interact/bird_adk_runtime.py` — the harness-owned wrapper that lets your `agent/agent.py` be served as the BIRD system agent via FastAPI.
- `agent/templates/bird_interact.py` — faithful copy of the stock BIRD-Interact-ADK system agent, copied to `agent/agent.py` by `prepare.py` as the iteration starting point.
- `program_templates/bird_interact.md` — benchmark-specific guidance appended to `PROGRAM.md`.

**Known caveats:**
- GPT-5-family models reject explicit `temperature=0`; the template omits the temperature kwarg for those models (stock behavior preserved for all other models).
- `prepare.py` creates a separate `.venv-adk` inside `bird_interact_adk/` because the ADK's deps (google-adk, psycopg2, etc.) may conflict with other benchmarks' deps.
- Advanced users can point at an existing BIRD-Interact install via `bird_repo` + `bird_python_bin` in `experiment_config.yaml` to skip auto-provisioning.

## Quick start: tau-bench

**Requirements:** Docker, an `OPENAI_API_KEY`, and a coding agent.

```bash
# 1. Clone the repo
git clone https://github.com/neosigmaai/auto-harness
cd auto-harness

# 2. Set up environment variables
cp .env.example .env
# edit .env — set OPENAI_API_KEY

# 3. Configure the experiment
cp experiment_config.yaml.template experiment_config.yaml
# edit experiment_config.yaml — uncomment the tau-bench section

# 4. Build the Docker image (installs tau-bench and all deps via uv)
docker compose build autoeval

# 5. Initialize the workspace + run baseline
docker compose run autoeval python prepare.py

# 6. Start the optimization loop
# Point your coding agent at the repo and prompt:
#   "Read PROGRAM.md and start the optimization loop."
```

---

## Running the loop

Point your coding agent at the repo and prompt:

```
Read PROGRAM.md and start the optimization loop.
The baseline is already recorded. Start from step 2 (analyze failures).
```

The agent will read traces, diagnose failures, edit `agent/agent.py`, gate the change, record the result, and repeat.

---

## How benchmarks are structured

### Templates

Each benchmark has two templates:

```
agent/templates/
├── tau_bench.py           # tau-bench agent starting point
├── terminal_bench.py      # terminal-bench agent starting point
└── bird_interact.py       # BIRD-Interact system agent starting point

program_templates/
├── tau_bench.md           # tau-bench PROGRAM.md
├── terminal_bench.md      # terminal-bench PROGRAM.md
└── bird_interact.md       # BIRD-Interact PROGRAM.md
```

`prepare.py` copies the correct templates into `agent/agent.py` and `PROGRAM.md` based on `experiment_config.yaml`. The coding agent then edits `agent/agent.py` freely. To see what it changed:

```bash
diff agent/templates/terminal_bench.py agent/agent.py
```

### Using a different Harbor benchmark

If your benchmark runs via `harbor run`, you only need four steps:

**1. Point to your dataset in `experiment_config.yaml`:**

```yaml
benchmark: "terminal-bench"   # reuses TerminalBenchRunner
dataset: "my-harbor-dataset@1.0"
agent_model: "gpt-4o"
env_provider: "e2b"           # or "daytona" / "modal" / "docker"
split: "train"
gate_split: "test"
```

**2. Check your verifier's `result.json` schema.**
`TerminalBenchRunner` expects:

```json
{
  "task_name": "<id>",
  "verifier_result": {
    "rewards": { "reward": 0.85 }
  }
}
```

If your verifier writes rewards at a different path, update the parser in `TerminalBenchRunner.run()` in `benchmark.py`.

**3. Update the split directory name (optional).**
The split file is currently saved to `tbench_data/task_split.json`. If you want a separate directory per benchmark, change `SPLIT_FILE` in `TerminalBenchRunner` and update `prepare.py` accordingly.

**4. Add a PROGRAM.md supplement.**
Create `program_templates/<your_benchmark>.md` with benchmark-specific guidance (trace paths, task ID format, known techniques) following the same pattern as `terminal_bench.md`. Then register it in `copy_program_template()` in `prepare.py`.

The train/test split generation, gating, trace copying, and optimization loop all work as-is — no other changes needed.

---

### Plugging in your own benchmark

Subclass `BenchmarkRunner` in `benchmark.py`:

```python
class MyBenchmarkRunner(BenchmarkRunner):
    def run(self, task_ids=None):
        # call your benchmark CLI or API
        # return {task_id: reward} where reward is 0.0–1.0
        ...
```

Add a branch in `gating.py`'s `_create_runners()` and `prepare.py`'s `__main__`. Create templates in `agent/templates/` and `program_templates/`. The loop, gating, recording, and workspace format are all benchmark-agnostic.

---

## Eval suite

The coding agent self-maintains `workspace/suite.json` — task IDs it must always pass.

`gating.py` runs three steps before any change is committed:

1. **Regression suite**: tasks in `suite.json` must pass at ≥ threshold (default 80%)
2. **Full test**: full benchmark on the test split; mean reward must be ≥ the best score seen so far
3. **Suite promotion**: previously-failing tasks that now pass are added to the suite

Steps 1 and 2 run sequentially; Step 2 always runs regardless of Step 1's outcome.

---

## Project structure

```
agent/
  agent.py                  the agent under optimization — only file the coding agent edits
  templates/                read-only starting points for each benchmark
  helpers/
    bird_interact/
      bird_service.py       FastAPI service wrapper for BIRD-Interact system agent
      bird_adk_runtime.py   Google ADK runtime adapter for the BIRD service
      setup.py              prepare.py helpers for BIRD-Interact provisioning
benchmark.py                benchmark execution layer (abstract + tau-bench + terminal-bench + bird-interact)
gating.py                   three-step gate (regression suite → full test → suite promotion)
prepare.py                  workspace setup, template copying, baseline run
record.py                   appends iteration result to results.tsv
PROGRAM.md                  loop instructions for the coding agent (copied from template)
program_templates/          benchmark-specific PROGRAM.md templates
experiment_config.yaml.template   example configs for each benchmark
Dockerfile                  container definition (tau-bench; `service` target for service/)
docker-compose.yml          autoeval (profile `loop`), plus postgres, api, and worker for service/
test_client.py              submits a job to service/ and prints its result and history
workspace/
  suite.json                regression eval suite (task IDs + threshold)
  learnings.md              per-run log: patterns, what worked, requests to human
  results.tsv               iteration history (val_score, commit, evals, timestamp)
  traces/                   agent conversation traces for failure analysis
```

---

## Design

- **Program the loop, not the agent directly.** The human steers through `PROGRAM.md`; the coding agent edits `agent/agent.py`.
- **Benchmark-agnostic loop.** The same gating, recording, and workspace format works for any benchmark that returns per-task rewards.
- **Self-maintained evals.** The coding agent decides which tasks belong in the regression suite — no manual curation needed.
- **Learnings close the feedback loop.** After each iteration the agent writes `workspace/learnings.md`: what it tried, what worked, what it needs from the human.
- **Gate everything.** No change is committed without passing both the eval suite and the full test score gate.
- **Structural anti-cheating.** Test traces are not saved to disk. The coding agent can only read train traces.

---

## Agent optimization service

`service/` is an HTTP service that benchmarks an agent on Terminal-Bench tasks and improves it with a meta-agent. It is separate from the coding-agent loop above. It runs Harbor through `TerminalBenchRunner` from `benchmark.py`, inside an E2B sandbox.

### Run

The service optimizes 1 agent: the git repo at `agent_store/agent`. Put the agent there before starting the service. To use this repo's Terminal-Bench agent as a 1-file repo:

```bash
mkdir -p agent_store/agent
cp agent/templates/terminal_bench.py agent_store/agent/agent.py
git -C agent_store/agent init --initial-branch=main
git -C agent_store/agent add agent.py
git -C agent_store/agent commit -m "Terminal-Bench agent"
```

Any other agent works the same way: `git clone <url> agent_store/agent`.

Put `E2B_API_KEY` and `OPENAI_API_KEY` in `.env`, then start the stack:

```bash
docker compose up -d --build
```

This starts 3 containers:

- `postgres`

- `api` on `http://localhost:8000`, with interactive docs at `/docs`

- `worker`, which runs queued jobs

On startup, the worker builds the E2B template `auto-harness-runner` in your E2B account. The first build takes about 1 minute. Later builds reuse E2B's cache.

Submit a job, poll it, and print the result and iteration history:

```bash
pip install httpx
python test_client.py --max-iterations 3 --tasks crack-7z-hash pypi-server
```

`test_client.py --help` lists the options. By default it optimizes `agent:HarnessAgent` at `main` on all tasks in `service/tasks.yaml`.

Worker settings, all optional:

- `AGENT_MODEL`: the model the agent uses. Default `gpt-5.4`.

- `OPTIMIZER_MODEL`: the meta-agent's model. Default `gpt-5.4`. It must be an OpenAI chat model with tool calling.

### API

```
POST /jobs                   queue a job; returns 202 and the job
GET  /jobs/{id}              status, latest commit and result, stop reason
GET  /jobs/{id}/iterations   every iteration: commit, parent, meta-agent analysis, diff, result
GET  /jobs/{id}/transcript   the meta-agent's conversation: every message, tool call, and tool output
```

`POST /jobs` body:

```json
{
  "agent": {
    "ref": "main",
    "entrypoint": "agent:HarnessAgent"
  },
  "task_ids": ["crack-7z-hash", "pypi-server"],
  "max_iterations": 3,
  "target_score": 0.8
}
```

- `ref` is a branch, tag, or commit in `agent_store/agent`. `entrypoint` is a Harbor import path from the repository root.

- `task_ids` defaults to all tasks in `service/tasks.yaml`.

- `max_iterations` (0 to 10, default 0) is the number of meta-agent improvement rounds after the first run.

- `target_score` (above 0, up to 1, default 1) is the pass rate that ends the loop early.

Job `status` is `queued`, `running`, `completed`, or `crashed`. `crashed` means the service could not produce a result, for example because Harbor produced no output. Failed tasks are part of a `completed` job. Each task in `result.tasks` is `passed` (reward 0.5 or more), `failed`, or `error` (no verifier result). Failed and errored tasks carry the tail of their test or error output.

`stop_reason` is `target_score` when the latest iteration's score reached `target_score`, and `max_iterations` otherwise.

### Tasks

[`service/tasks.yaml`](service/tasks.yaml) names the Harbor dataset (`terminal-bench@2.0`) and 20 default tasks. We picked them in 2 steps:

1. We considered medium and hard tasks with an agent timeout of 900s or less. We left out tasks that download models, need 4G of memory, run QEMU, or have expert estimates of 8 hours or more. From those, we picked 20. They include the 4 tasks the agent failed in an earlier 15-task run, `overfull-hbox` (easy) among them. They exclude the 11 tasks it passed.

2. We ran this repo's Terminal-Bench agent on the 20 tasks in E2B 3 times, all at the same commit. It scored 0.50, 0.40, and 0.35. In the first run, 8 tasks failed their tests. 2 (`crack-7z-hash` and `query-optimize`) errored because the agent does not catch E2B's timeout on a command that runs over 120s.

The set covers 10 categories: 15 tasks are medium, 4 are hard, and 1 is easy. Expert time estimates range from 5 to 180 minutes, with a median of 60. The tasks the agent fails give the optimizer something to fix. The tasks it passes catch regressions.

Harbor runs all tasks of a job in parallel and applies each task's own timeouts from its `task.toml`. `TerminalBenchRunner` kills Harbor after 1 hour as a last resort.

### Design

- **Agents are git commits.** The operator places the agent repo at `agent_store/agent`, which compose bind-mounts into the api and worker. The API resolves `ref` in that repo and pins the job to the commit. Each meta-agent change is a new commit on top of the commit the meta-agent chose. Its message holds the meta-agent's analysis. Postgres stores only commit hashes. Ref `refs/jobs/<job_id>/<index>` keeps every iteration's commit, so `git log refs/jobs/<job_id>/<index>` and `git diff` work on the host. Back up `agent_store` together with Postgres.

- **Postgres is the queue.** `POST /jobs` inserts a `queued` row. The worker claims the oldest one with `SELECT ... FOR UPDATE SKIP LOCKED`. The API and worker share only Postgres and the agent store.

- **3 tables** in `service/schemas.py`. `jobs` holds the request, status, and stop reason. `iterations` holds 1 row per benchmark run: commit, parent commit, and result. `meta_messages` holds the meta-agent's conversation, 1 row per message, appended as each message is added. The meta-agent can read `jobs` and `iterations` of every job.

- **Loop.** The worker benchmarks the base commit as iteration 0. Then it runs a meta-agent: an OpenAI tool-calling loop in `service/optimizer.py`. Each iteration, the meta-agent gets a fresh E2B sandbox with a git checkout of the agent repo. It has 2 tools:

  - `bash` runs in the checkout, in the sandbox. The worker uploads a git bundle of every ref in the agent repo, so the meta-agent can read every commit and diff. The sandbox gets no API keys and no database URL.

  - `sql` runs 1 read-only statement on Postgres as the role `meta_agent`. The role can read only `jobs` and `iterations`. The meta-agent can compare per-task results across commits and jobs.

  The meta-agent keeps 1 conversation for the whole job. It starts by reading the agent's code and control flow. Each iteration adds 1 message: the previous iteration's outcome and the commit the checkout is at. The service keeps no notion of a best commit. The meta-agent reads commits and scores from git and the database, decides which commit to build on, and checks it out. It edits any files and ends with a text reply. The sandbox commits the checkout on top of the checked-out commit, with that reply as the message. The worker fetches the new commit into the agent repo as a git bundle, deletes the sandbox, and benchmarks the commit. Every change is committed and benchmarked, and the next iteration's checkout starts at the new commit. An attempt that fails or changes no files uses up its iteration without a commit, and the next checkout starts at the previous commit. The loop stops when an iteration's score reaches `target_score` or after `max_iterations` iterations. The system prompt reuses the failure checklist and the known techniques from [`program_templates/terminal_bench.md`](program_templates/terminal_bench.md).

- **Sandbox boundary.** The worker runs no agent code. For each benchmark run, `service/runner.py` does 4 things:

  1. It creates 1 outer E2B sandbox from the template `auto-harness-runner`: Python 3.12, git, and `harbor[e2b]==0.23.0`. The sandbox gets only `E2B_API_KEY` and `OPENAI_API_KEY`.

  2. It uploads the commit as a tar, `benchmark.py`, and `service/sandbox_run.py`.

  3. It runs `sandbox_run.py` in the sandbox. That script runs Harbor, which creates 1 E2B sandbox per task. The agent's Python code (its LLM loop) runs in the Harbor process in the outer sandbox. Its commands and the verifier run in the task sandboxes. Harbor deletes each task sandbox when its task ends.

  4. It reads `report.json` (rewards and output tails) back and kills the outer sandbox.

- **Agent load errors.** Before Harbor starts, `sandbox_run.py` imports the entrypoint in the outer sandbox. If the import fails, every task gets status `error` with the traceback, and Harbor does not run. A change that breaks the import then scores 0, and the meta-agent can read why in the database.

### Not implemented

- **Multi-tenancy.** There are no orgs, users, or API keys yet.

- **Context limits.** The meta-agent's conversation grows with every tool call for the whole job. Bash output is cut to 8000 characters per call. SQL output is never cut. A long job can still exceed the model's context window, and every later iteration then fails. Summarizing earlier iterations would fix this.

- **Agent secrets and results.** The agent's code runs in the same outer sandbox as Harbor. It can read `E2B_API_KEY` and `OPENAI_API_KEY`, and it could overwrite Harbor's result files. Fixing this needs a model proxy with per-run keys, and the verifier's results read from outside the outer sandbox.

- **Several agents.** The service has 1 agent repo. Supporting several needs an agent ID in the request and 1 repo per agent.

- **Multiple workers.** On startup, the worker marks every `running` job as `crashed`, so only 1 worker may run. Leases with heartbeats would allow several.

- **Sandbox cleanup after a kill.** E2B deletes the outer sandbox 70 minutes after it starts, even if the worker died. Harbor creates task sandboxes with a 24-hour lifetime. If Harbor is killed mid-run, its task sandboxes keep running until E2B stops them.

### With more time

- Worker leases, retries, and job cancellation.

- Held-out tasks to detect overfitting, and several attempts per task to reduce noise.

- Keep each run's Harbor trial logs so the meta-agent can read the agent's full trajectories. It sees only the tail of each failed task's output now.

- Alembic migrations. Tables are created on startup, so a schema change needs a fresh database.

### Test

Requires pip 25.1 or newer for `--group`.

```bash
python -m venv .venv
.venv/bin/pip install --group service
docker compose up -d postgres
.venv/bin/python -m pytest service/tests
```

Tests use a separate `harness_test` database and create it if missing. They replace Harbor and the meta-agent's model with stubs, so they need no API keys.
