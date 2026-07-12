"""Pure parsers for squeue/sacct output and Slurm state semantics."""

from __future__ import annotations

import re
import time

from slurmdeck.models.status import SchedulerObservation, SchedulerSource

TERMINAL_STATES = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "REVOKED",
}
ACTIVE_STATES = {"PENDING", "RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED", "REQUEUED", "RESIZING", "STAGE_OUT"}


def failed_states() -> set[str]:
    return TERMINAL_STATES - {"COMPLETED"}


def normalize_state(raw: str) -> str:
    """``CANCELLED by 1234`` → ``CANCELLED``; ``COMPLETED+`` → ``COMPLETED``."""
    state = raw.strip().split()[0] if raw.strip() else ""
    return state.rstrip("+")


def _normalize_reason(raw: str) -> str:
    reason = raw.strip()
    return "" if reason in {"None", "(None)", "N/A"} else reason


_ARRAY_SPEC = re.compile(r"^(?P<base>\d+)_\[(?P<spec>[^\]]+)\](?:%\d+)?$")


def expand_array_id(job_id: str) -> list[str]:
    """Expand ``123_[0-2,5]%4`` into ``123_0 123_1 123_2 123_5``.

    Plain ids pass through; job-step ids (``123.batch``) are dropped.
    """
    job_id = job_id.strip()
    if not job_id or "." in job_id:
        return []
    match = _ARRAY_SPEC.match(job_id)
    if not match:
        return [job_id]
    base = match.group("base")
    spec = match.group("spec")
    spec_without_limit, separator, limit = spec.rpartition("%")
    if separator and limit.isdigit():
        spec = spec_without_limit
    expanded: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        step = 1
        if ":" in part:
            part, step_text = part.split(":", 1)
            step = int(step_text)
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            expanded.extend(f"{base}_{index}" for index in range(int(start_text), int(end_text) + 1, step))
        else:
            expanded.append(f"{base}_{part}")
    return expanded


def _identity(expanded_job_id: str) -> tuple[str, str | None]:
    job_id, separator, array_task_id = expanded_job_id.partition("_")
    return job_id, array_task_id if separator else None


def parse_squeue(output: str, *, observed_at: float | None = None) -> dict[str, SchedulerObservation]:
    """Parse ``squeue -h -o '%i|%T|%R'`` output, expanding compressed array ids."""
    observed_at = time.time() if observed_at is None else observed_at
    records: dict[str, SchedulerObservation] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        job_id, _, rest = line.partition("|")
        state, _, reason = rest.partition("|")
        for expanded in expand_array_id(job_id):
            base_job_id, array_task_id = _identity(expanded)
            records[expanded] = SchedulerObservation(
                job_id=base_job_id,
                array_task_id=array_task_id,
                scheduler_state=normalize_state(state),
                scheduler_reason=_normalize_reason(reason),
                observed_at=observed_at,
                source=SchedulerSource.SQUEUE,
            )
    return records


def parse_sacct(output: str, *, observed_at: float | None = None) -> dict[str, SchedulerObservation]:
    """Parse ``sacct -n -P --format=JobID,State,ExitCode,Reason`` output."""
    observed_at = time.time() if observed_at is None else observed_at
    records: dict[str, SchedulerObservation] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("|")
        if len(fields) < 3:
            continue
        job_id, state, exit_code = fields[0], fields[1], fields[2]
        reason = fields[3] if len(fields) > 3 else ""
        if "." in job_id:  # skip .batch/.extern steps
            continue
        for expanded in expand_array_id(job_id):
            base_job_id, array_task_id = _identity(expanded)
            records[expanded] = SchedulerObservation(
                job_id=base_job_id,
                array_task_id=array_task_id,
                scheduler_state=normalize_state(state),
                scheduler_reason=_normalize_reason(reason),
                exit_code=exit_code,
                observed_at=observed_at,
                source=SchedulerSource.SACCT,
            )
    return records


def parse_sbatch_parsable(output: str) -> str:
    """``sbatch --parsable`` prints ``<jobid>[;cluster]`` on the last non-empty line."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line:
            job_id = line.split(";", 1)[0]
            if job_id.isdigit():
                return job_id
    raise ValueError(f"could not parse sbatch --parsable output: {output!r}")
