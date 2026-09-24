import json
from collections.abc import Callable
from pathlib import Path

from openai import OpenAI
from sqlalchemy import create_engine

from service.config import META_DB_PASSWORD, META_DB_USER, OPTIMIZER_MODEL
from service.schemas import META_AGENT_TABLES, Base, engine
from service.checkout import Checkout

MAX_STEPS = 40  # tool-calling rounds per iteration
OUTPUT_CHARS = 8000  # bash output kept per call
# The `sql` tool connects as the meta-agent's role, so Postgres rejects every table outside META_AGENT_TABLES.
meta_engine = create_engine(engine.url.set(username=META_DB_USER, password=META_DB_PASSWORD), pool_pre_ping=True)
# Guidance written for the repo's coding-agent loop (see prepare.py), reused here.
PROGRAM_TEMPLATE = Path(__file__).parent.parent / "program_templates" / "terminal_bench.md"


def _template_items(heading: str) -> str:
    """The bullet and numbered lines under `### <heading>` in PROGRAM_TEMPLATE."""
    parts = PROGRAM_TEMPLATE.read_text().split(f"\n### {heading}\n")
    if len(parts) != 2:
        raise ValueError(f"{PROGRAM_TEMPLATE} has no section '### {heading}'")
    section = parts[1].split("\n### ")[0]
    items = [line for line in section.splitlines() if line.startswith("- ") or line[:1].isdigit()]
    if not items:
        raise ValueError(f"section '### {heading}' in {PROGRAM_TEMPLATE} has no list items")
    return "\n".join(items)


def _schema() -> str:
    return "\n".join(
        f"  {table.name}({', '.join(f'{c.name} {c.type.compile(dialect=engine.dialect)}' for c in table.columns)})"
        for table in Base.metadata.sorted_tables if table.name in META_AGENT_TABLES
    )


# Built at import, so a renamed template section stops the worker at startup.
SYSTEM_PROMPT = f"""You improve an AI agent that solves Terminal-Bench tasks in a Linux sandbox.
Your working directory is a git checkout of the agent. At the start of the job, before changing anything,
read the agent's code and its control flow: how it calls the model, how it runs commands, and when it stops.
In each iteration:
1. Decide which commit to build on from the commits and their benchmark results, and check it out
   with `git checkout --detach <commit>`. After an iteration, the checkout is at that iteration's commit.
2. Edit any files to make the agent better. Do not commit yourself. Put scratch files outside the checkout.
   Each iteration starts in a fresh sandbox, so files outside the checkout do not carry over.
3. Reply without a tool call. The service commits the checkout on top of the commit you checked out,
   and benchmarks it. The result appears in the database.

Tools:
- bash: runs in the checkout, in a sandbox with Python 3.12, git, and Harbor. Every version is kept under refs/jobs/<job_id>/<index>, and
  `git log --all`, `git show <commit>:<path>`, and `git diff` work. The message of each
  optimizer commit is the analysis of that attempt.
- sql: 1 read-only statement on the service's Postgres. Tables:
{_schema()}
  `result` is {{"score": fraction passed, "tasks": [{{"task_id", "status": "passed" | "failed" | "error",
  "reward", "failure": tail of the test or error output}}]}}. `iterations.parent` is the commit an
  iteration built on. Results of other jobs on the same repo are there too.
  Scores come from 1 run per task and vary by a few tasks between runs of the same commit.

For each failed task, consider:
{_template_items("Analyzing Failures (Step 2)")}

Techniques known to improve Terminal-Bench scores:
{_template_items("Known Techniques That Improve Terminal-Bench Scores")}

Make 1 focused change per iteration. Do not repeat an attempt that did not help. Do not special-case
individual tasks. Keep the entrypoint's class name and interface.
When the change is done, reply without a tool call: the cause you found and the change you made,
in a few sentences. That reply becomes the commit message."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a bash command in the agent checkout. Returns stdout, stderr, and the exit code.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql",
            "description": "Run 1 read-only SQL statement on the service's Postgres. Returns every row as JSON.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


class MetaAgent:
    """A tool-using LLM that improves the agent. It keeps 1 conversation for the whole job.

    `record` receives every message as it is added to the conversation.
    """

    def __init__(self, record: Callable[[dict], None]):
        self.record = record
        self.messages: list[dict] = []
        self.client = OpenAI()
        self._add({"role": "system", "content": SYSTEM_PROMPT})

    def run(self, message: str, work: Checkout) -> str:
        """Add `message` to the conversation and work in `work` until the model replies without a tool call.

        Return that reply.
        """
        self._add({"role": "user", "content": message})
        for _ in range(MAX_STEPS):
            reply = self.client.chat.completions.create(model=OPTIMIZER_MODEL, messages=self.messages, tools=TOOLS)
            msg = reply.choices[0].message
            self._add(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                if not (msg.content or "").strip():
                    raise ValueError("empty final reply")
                return msg.content
            for call in msg.tool_calls:
                self._add({"role": "tool", "tool_call_id": call.id, "content": self._tool(call, work)})
        raise RuntimeError(f"no final reply after {MAX_STEPS} steps")

    def _add(self, message: dict) -> None:
        self.messages.append(message)
        self.record(message)

    def _tool(self, call, work: Checkout) -> str:
        # Never raises: every tool call needs a reply, or the conversation becomes invalid for later iterations.
        try:
            args = json.loads(call.function.arguments)
            if call.function.name == "bash":
                return _truncate(work.bash(args["command"]))
            if call.function.name == "sql":
                return sql(args["query"])
            return f"unknown tool {call.function.name!r}"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"


def sql(query: str) -> str:
    """Run `query` as the meta-agent's role, in a read-only transaction."""
    conn = meta_engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '10s'")
        # A prepared statement holds exactly 1 command, so `COMMIT; ...` cannot leave the read-only transaction.
        # No parameters are passed, so a literal % is escaped.
        cur.execute(query.replace("%", "%%"), (), prepare=True)
        if cur.description is None:
            return "(no rows)"
        columns = [d[0] for d in cur.description]
        return json.dumps([dict(zip(columns, row)) for row in cur.fetchall()], default=str)
    finally:
        conn.rollback()
        conn.close()


def _truncate(text: str) -> str:
    if len(text) <= OUTPUT_CHARS:
        return text
    half = OUTPUT_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - OUTPUT_CHARS} chars cut] ...\n{text[-half:]}"
