# ── Agent optimization service (service/) ─────────────────────────────────────
# Build with `--target service`. Installs only the `service` dependency group.
FROM python:3.12-slim AS service

WORKDIR /app
# git reads and writes agent versions in the agent repo. The repo is bind-mounted
# from the host with the host's owner, so git must trust it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'
# `pip install --group` needs pip 25.1 or newer.
RUN pip install --no-cache-dir --upgrade "pip>=25.1"
COPY pyproject.toml pyproject.toml
RUN pip install --no-cache-dir --group service

# The worker runs Terminal-Bench through benchmark.py's TerminalBenchRunner.
COPY benchmark.py benchmark.py
# The optimizer's prompt reuses guidance from the coding-agent loop's template.
COPY program_templates/terminal_bench.md program_templates/terminal_bench.md
COPY service service

CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Coding-agent loop (default target, last stage) ────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install git (needed for uv to fetch tau2 from git) and uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

# Copy dependency manifest first for better layer caching
COPY pyproject.toml ./

# Install all dependencies (including tau2 from git) into a venv
RUN uv sync --no-dev

# Activate venv so plain `python` resolves to the venv interpreter
ENV PATH="/app/.venv/bin:$PATH"

# Copy project files
COPY . .

# workspace/ is mounted at runtime — create as fallback
RUN mkdir -p workspace

CMD ["bash"]
