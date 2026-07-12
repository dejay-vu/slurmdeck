"""Builders for the Slurm commands slurmdeck runs on the remote (pure strings)."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def sbatch_command(script_path: str, *, chdir: str | None = None) -> str:
    parts = ["sbatch", "--parsable"]
    if chdir:
        parts.extend(["--chdir", shlex.quote(chdir)])
    parts.append(shlex.quote(script_path))
    return " ".join(parts)


def squeue_command(job_ids: Sequence[str]) -> str:
    ids = ",".join(job_ids)
    return f"squeue -h -o '%i|%T|%R' -j {shlex.quote(ids)}"


def sacct_command(job_ids: Sequence[str]) -> str:
    ids = ",".join(job_ids)
    return f"sacct -n -P --format=JobID,State,ExitCode,Reason -j {shlex.quote(ids)}"


def scancel_command(job_ids: Sequence[str]) -> str:
    return "scancel " + " ".join(shlex.quote(job_id) for job_id in job_ids)
