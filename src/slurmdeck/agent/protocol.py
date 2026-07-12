"""Local-side view of the agent protocol: constants, file names, and source access.

The agent itself (``agent.py``) is stdlib-only and cannot import slurmdeck, so
these constants are duplicated there by design; ``tests/unit/test_agent_*``
assert the two stay in sync.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

AGENT_VERSION = 1
STATUS_SCHEMA_VERSION = 1
JSON_PREFIX = "SLURMDECK_JSON\t"
SCAN_KIND_HEADER = "slurmdeck.scan.v1"
SCAN_KIND_TASK = "task"
SCAN_KIND_SQUEUE = "squeue"
SCAN_KIND_SACCT = "sacct"
SCAN_KIND_ENV_DEPENDENCY = "env_dependency"
RUN_SUBMISSION_KIND = "slurmdeck.run-submission.v1"
RUN_RECEIPT_SCHEMA_VERSION = 1
RUN_CLEAN_KIND = "slurmdeck.run-clean.v1"
SNAPSHOT_LIFECYCLE_KIND = "slurmdeck.snapshot-lifecycle.v1"

AGENT_FILE = "agent.py"
ENV_AGENT_FILE = "env_agent.py"
ENV_HELPER_KIND = "slurmdeck.env-registry.v1"
TASKS_FILE = "tasks.jsonl"
RUN_MANIFEST_FILE = "run.json"
ACTIVATION_FILE = "activation.sh"
SBATCH_FILE = "submit.sbatch"
CONFIGS_DIR = "configs"
LOGS_DIR = "logs"
RESULTS_DIR = "results"
STATUS_FILE = "status.json"


@lru_cache(maxsize=1)
def agent_source() -> str:
    """The packaged agent source, for per-run upload and ssh-stdin invocation."""
    return (resources.files("slurmdeck.agent") / AGENT_FILE).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def env_agent_source() -> str:
    """The packaged stdlib-only environment registry helper."""
    return (resources.files("slurmdeck.agent") / ENV_AGENT_FILE).read_text(encoding="utf-8")
