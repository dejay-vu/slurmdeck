"""Business states mapped to theme-independent semantic roles."""

from __future__ import annotations

STATE_STYLES: dict[str, str] = {
    "COMPLETED": "state.success",
    "CONNECTED": "state.success",
    "OK": "state.success",
    "READY": "state.success",
    "SUBMITTED": "state.success",
    "TERMINAL": "state.success",
    "BUILDING": "state.active",
    "COMPLETING": "state.active",
    "CONFIGURING": "state.active",
    "PREPARING": "state.active",
    "RUNNING": "state.active",
    "STAGE_OUT": "state.active",
    "SUBMITTING": "state.active",
    "PENDING": "state.pending",
    "PLANNED": "state.pending",
    "QUEUED": "state.pending",
    "REQUEUED": "state.pending",
    "RESIZING": "state.pending",
    "SUSPENDED": "state.pending",
    "WAITING_FOR_ENV": "state.pending",
    "BOOT_FAIL": "state.failure",
    "CANCELLED": "state.failure",
    "DEADLINE": "state.failure",
    "ENV_BUILD_CANCELLED": "state.failure",
    "ENV_BUILD_FAILED": "state.failure",
    "FAILED": "state.failure",
    "KILLED": "state.failure",
    "NODE_FAIL": "state.failure",
    "OUT_OF_MEMORY": "state.failure",
    "PREEMPTED": "state.failure",
    "SUBMIT_FAILED": "state.failure",
    "TIMEOUT": "state.failure",
    "STALE": "state.warning",
    "WARN": "state.warning",
    "WARNING": "state.warning",
    "BUILD_UNKNOWN": "state.unknown",
    "SUBMIT_UNKNOWN": "state.unknown",
    "UNKNOWN": "state.unknown",
    "REMOVED": "state.muted",
    "SKIPPED": "state.muted",
}


def state_style(state: str) -> str | None:
    """Return a semantic style role for a public state label."""
    return STATE_STYLES.get(state.upper())
