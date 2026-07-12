"""Typed client for the stdlib-only remote run submission helper."""

from __future__ import annotations

from dataclasses import dataclass

from slurmdeck.agent import protocol
from slurmdeck.models.cluster import InvalidDependencyPolicy
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.repos import RunRow
from slurmdeck.transport import Transport, TransportError, parse_json_lines

_SUBMISSION_STATUSES = {"submitted", "failed", "unknown"}


@dataclass(frozen=True)
class RemoteSubmissionResult:
    status: str
    token: str
    run_id: str
    job_name: str
    job_id: str
    source: str
    error: str


@dataclass(frozen=True)
class RemoteCleanResult:
    ok: bool
    removed_run: bool
    removed_receipt: bool
    error: str


class RunSubmissionClient:
    """Invoke and strictly validate the remote idempotency protocol."""

    @staticmethod
    def job_name(token: str) -> str:
        return f"sd-{token[-12:]}"

    def submit(
        self,
        transport: Transport,
        layout: RemoteLayout,
        run: RunRow,
        token: str,
        *,
        dependency_job_id: str = "",
        invalid_dependency_policy: InvalidDependencyPolicy | None = None,
    ) -> RemoteSubmissionResult:
        args = [
            "submit-run",
            "--base",
            layout.base,
            "--run-id",
            run.id,
            "--token",
            token,
            "--job-name",
            self.job_name(token),
            "--script",
            f"{run.remote_root}/{protocol.SBATCH_FILE}",
            "--snapshot-hash",
            run.snapshot_hash,
        ]
        if dependency_job_id:
            args += ["--dependency-job-id", dependency_job_id]
        if invalid_dependency_policy is not None:
            args += ["--invalid-dependency-policy", invalid_dependency_policy.value]
        return self._invoke(
            transport,
            args,
            run_id=run.id,
            token=token,
            timeout=180,
        )

    def reconcile(
        self,
        transport: Transport,
        layout: RemoteLayout,
        run: RunRow,
    ) -> RemoteSubmissionResult:
        return self._invoke(
            transport,
            [
                "reconcile-run",
                "--base",
                layout.base,
                "--run-id",
                run.id,
                "--token",
                run.submission_token,
                "--job-name",
                self.job_name(run.submission_token),
            ],
            run_id=run.id,
            token=run.submission_token,
            timeout=180,
        )

    def clean(self, transport: Transport, layout: RemoteLayout, run: RunRow) -> RemoteCleanResult:
        args = ["clean-run", "--base", layout.base, "--run-id", run.id]
        if run.submission_token:
            args += ["--token", run.submission_token]
        result = transport.exec_python(protocol.agent_source(), args, timeout=300, check=False)
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote run cleanup helper returned malformed JSON.",
                returncode=result.returncode,
                stderr=result.stderr,
                underlying_cause=exc,
            ) from exc
        payload = next(
            (
                item
                for item in reversed(payloads)
                if isinstance(item, dict) and item.get("kind") == protocol.RUN_CLEAN_KIND
            ),
            None,
        )
        if (
            payload is None
            or payload.get("schema_version") != 1
            or payload.get("run_id") != run.id
            or payload.get("token") != run.submission_token
        ):
            raise TransportError(
                "Remote run cleanup helper returned an invalid result contract.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        ok = payload.get("ok")
        removed_run = payload.get("removed_run")
        removed_receipt = payload.get("removed_receipt")
        error = payload.get("error")
        if (
            not isinstance(ok, bool)
            or not isinstance(removed_run, bool)
            or not isinstance(removed_receipt, bool)
            or not isinstance(error, str)
        ):
            raise TransportError(
                "Remote run cleanup helper returned invalid outcome fields.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        if result.returncode != 0:
            raise TransportError(
                "Remote run cleanup helper exited unsuccessfully.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return RemoteCleanResult(
            ok=ok,
            removed_run=removed_run,
            removed_receipt=removed_receipt,
            error=error,
        )

    @staticmethod
    def _invoke(
        transport: Transport,
        args: list[str],
        *,
        run_id: str,
        token: str,
        timeout: float,
    ) -> RemoteSubmissionResult:
        result = transport.exec_python(protocol.agent_source(), args, timeout=timeout, check=False)
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote submission helper returned malformed JSON.",
                returncode=result.returncode,
                stderr=result.stderr,
                underlying_cause=exc,
            ) from exc
        payload = next(
            (
                item
                for item in reversed(payloads)
                if isinstance(item, dict) and item.get("kind") == protocol.RUN_SUBMISSION_KIND
            ),
            None,
        )
        if payload is None:
            raise TransportError(
                "Remote submission helper produced no structured result.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        status = payload.get("status")
        if (
            payload.get("schema_version") != protocol.RUN_RECEIPT_SCHEMA_VERSION
            or payload.get("token") != token
            or payload.get("run_id") != run_id
            or status not in _SUBMISSION_STATUSES
        ):
            raise TransportError(
                "Remote submission helper returned an invalid result contract.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        job_id = payload.get("job_id", "")
        if status == "submitted" and (not isinstance(job_id, str) or not job_id.isdigit()):
            raise TransportError(
                "Remote submission helper returned an invalid Slurm job id.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return RemoteSubmissionResult(
            status=str(status),
            token=token,
            run_id=run_id,
            job_name=str(payload.get("job_name", "")),
            job_id=str(job_id),
            source=str(payload.get("source", "")),
            error=str(payload.get("error", "")),
        )
