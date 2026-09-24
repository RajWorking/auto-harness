import os
from pathlib import Path

import yaml

DATABASE_URL = os.environ.get("SERVICE_DATABASE_URL", "postgresql+psycopg://harness:harness@localhost:5432/harness")
_tasks = yaml.safe_load(Path(__file__).with_name("tasks.yaml").read_text())
DATASET: str = _tasks["dataset"]  # Harbor dataset, passed to `harbor run -d`
TASK_IDS: list[str] = _tasks["tasks"]
# Login role of the meta-agent's `sql` tool. init_db lets it read only META_AGENT_TABLES.
META_DB_USER = "meta_agent"
META_DB_PASSWORD = os.environ.get("SERVICE_META_DB_PASSWORD", "meta_agent")
# Git clone of the agent being optimized. The operator clones it here before starting the service.
AGENT_REPO = Path(os.environ.get("SERVICE_AGENT_REPO", Path(__file__).parent.parent / "agent_store" / "agent"))

# Worker settings.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-5.4")
OPTIMIZER_MODEL = os.environ.get("OPTIMIZER_MODEL", "gpt-5.4")
