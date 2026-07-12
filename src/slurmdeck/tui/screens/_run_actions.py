"""Guarded run actions shared by the runs dashboard and the run-detail screen."""

from __future__ import annotations

from pathlib import Path

from slurmdeck.storage.repos import RunRow
from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.widgets import InputModal


def confirm_submit(screen: DeckScreen, row: RunRow) -> None:
    if row.state not in {"planned", "submit_failed"}:
        screen.notify(
            f"{row.id} is {row.state}; only planned or failed submissions can be submitted.",
            severity="warning",
        )
        return
    screen.confirm(
        f"Submit {row.id}?",
        lambda: screen.controller.submit_planned(row.id),
        detail=f"{row.summary.total or 'its'} task(s) will be queued on {row.remote}.",
    )


def confirm_cancel(screen: DeckScreen, row: RunRow) -> None:
    if row.state != "submitted":
        screen.notify(f"{row.id} is {row.state}; only submitted runs can be cancelled.", severity="warning")
        return
    screen.confirm(
        f"Cancel {row.id}?",
        lambda: screen.controller.cancel_run(row.id),
        detail=f"scancel job {row.slurm_job_id} on {row.remote}.",
    )


def confirm_retry(screen: DeckScreen, row: RunRow) -> None:
    screen.confirm(
        f"Retry failed tasks of {row.id}?",
        lambda: screen.controller.retry_run(row.id),
        detail="Status is refreshed first; a new run is planned and submitted.",
    )


def prompt_pull(screen: DeckScreen, row: RunRow) -> None:
    def on_path(path: str | None) -> None:
        if path:
            screen.controller.pull_run(row.id, Path(path))

    screen.app.push_screen(InputModal(f"Pull {row.id} into:", value=f"pulled/{row.id}"), on_path)


def confirm_clean(screen: DeckScreen, row: RunRow) -> None:
    detail = "Deletes the local run record and directory"
    if row.remote_root:
        detail += f" and {row.remote_root} on {row.remote}"
    screen.confirm(f"Clean {row.id}?", lambda: screen.controller.clean_run(row.id), detail=detail + ".")
