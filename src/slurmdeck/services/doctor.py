"""Read-only readiness diagnosis for the local setup, remote, and project."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import ClusterObservation
from slurmdeck.models.remote import Remote
from slurmdeck.operations import OperationPhase, OperationReporter, OperationSink, noop_operation_sink
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.context import AppContext
from slurmdeck.storage.db import DB_SCHEMA_VERSION
from slurmdeck.transport import Transport, TransportError
from slurmdeck.transport.ssh import clean_child_env


@dataclass(frozen=True)
class Check:
    name: str
    state: str  # OK | WARN | FAILED | SKIPPED
    detail: str
    fix: str = ""


class DoctorService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def run(
        self,
        *,
        remote_name: str | None = None,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> list[Check]:
        reporter = OperationReporter("remote.doctor", operation_sink)
        reporter.started(OperationPhase.PROBE, message="Checking local tools")
        try:
            checks = self._run_checks(remote_name=remote_name, reporter=reporter)
        except Exception as exc:
            reporter.failed(message=str(exc))
            raise
        reporter.completed(
            OperationPhase.VALIDATE,
            message="Diagnosis complete",
            result_counts={
                "checks": len(checks),
                "failed": sum(check.state == "FAILED" for check in checks),
                "warnings": sum(check.state == "WARN" for check in checks),
            },
        )
        return checks

    def _run_checks(self, *, remote_name: str | None, reporter: OperationReporter) -> list[Check]:
        checks = self._local_tool_checks()
        reporter.started(OperationPhase.CONNECT, message="Resolving remote configuration")
        transport: Transport | None = None
        try:
            remote = self._ctx.resolve_remote(remote_name)
            checks.append(Check("remote", "OK", f"{remote.name} ({remote.destination})"))
            transport = self._ctx.transport(remote)
        except UserError as exc:
            checks.append(Check("remote", "FAILED", exc.message, exc.hint or ""))
            remote = None

        if transport is None or remote is None:
            for name in ("connection", "remote python3", "slurm", "base", "cluster profile"):
                checks.append(Check(name, "SKIPPED", "no remote configured"))
        else:
            observation = None
            reporter.started(OperationPhase.PROBE, message=f"Probing cluster capabilities on {remote.name}")
            try:
                observation = ClusterCapabilityService().observe(transport, remote)
                master = "multiplexed" if transport.alive() else "direct (no ControlMaster session)"
                checks.append(Check("connection", "OK", f"{remote.destination}: reachable, {master}"))
            except TransportError as exc:
                if exc.returncode == 255 or exc.returncode is None:
                    checks.append(
                        Check("connection", "FAILED", str(exc).splitlines()[0], "Check `ssh <destination>` by hand.")
                    )
                    skip_reason = "remote unreachable"
                else:
                    checks.append(Check("connection", "OK", f"{remote.destination}: reachable"))
                    checks.append(
                        Check("remote python3", "FAILED", "python3 not runnable", "Install python3 on the remote.")
                    )
                    skip_reason = "needs remote python3"
                if not any(check.name == "remote python3" for check in checks):
                    checks.append(Check("remote python3", "SKIPPED", skip_reason))
                for name in ("slurm", "base", "cluster profile"):
                    checks.append(Check(name, "SKIPPED", skip_reason))

            if observation is not None:
                checks.extend(self._observation_checks(remote, observation))

        reporter.started(OperationPhase.VALIDATE, message="Checking project state")
        if self._ctx.project is None:
            checks.append(Check("project", "SKIPPED", "not inside a slurmdeck project", "Run `slurmdeck init`."))
        else:
            checks.append(Check("project", "OK", str(self._ctx.project.paths.state_dir)))
            checks.append(self._database_check())
            if self._ctx.project.config.env is None:
                checks.append(Check("environment", "SKIPPED", "no env configured in project.yaml"))
            else:
                checks.append(Check("environment", "OK", f"{self._ctx.project.config.env.type} env configured"))
        return checks

    @staticmethod
    def _local_tool_checks() -> list[Check]:
        checks: list[Check] = []
        for binary, version_flag in (("ssh", "-V"), ("rsync", "--version")):
            path = shutil.which(binary)
            if not path:
                checks.append(Check(binary, "FAILED", f"{binary} not found on PATH", f"Install {binary}."))
                continue
            try:
                probe = subprocess.run(
                    [binary, version_flag],
                    stdin=subprocess.DEVNULL,
                    env=clean_child_env(),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                checks.append(Check(binary, "FAILED", f"{path}: {exc}", f"Reinstall {binary}."))
                continue
            if probe.returncode == 0:
                banner = (probe.stdout or probe.stderr).strip().splitlines()
                checks.append(Check(binary, "OK", banner[0] if banner else path))
            else:
                detail = (probe.stderr or probe.stdout).strip().splitlines()
                checks.append(
                    Check(
                        binary,
                        "FAILED",
                        detail[0] if detail else f"{binary} exited with rc={probe.returncode}",
                        "Check for LD_LIBRARY_PATH/LD_PRELOAD overrides in your environment.",
                    )
                )
        return checks

    @staticmethod
    def _observation_checks(remote: Remote, observation: ClusterObservation) -> list[Check]:
        checks: list[Check] = []
        version = tuple(int(part) for part in observation.python_version.split("."))
        if version >= (3, 8):
            checks.append(Check("remote python3", "OK", observation.python_version))
        else:
            checks.append(
                Check(
                    "remote python3",
                    "FAILED",
                    f"python3 is {observation.python_version}, need >= 3.8",
                    "Load a newer python module by default on the remote.",
                )
            )

        missing = [
            name
            for name in ("sbatch", "squeue", "sacct", "scancel")
            if name not in observation.tools or not observation.tools[name].available
        ]
        if not missing:
            checks.append(Check("slurm", "OK", "sbatch/squeue/sacct/scancel available"))
        else:
            checks.append(Check("slurm", "FAILED", f"missing: {', '.join(missing)}", "Is this a Slurm login node?"))

        if not remote.resolved_base:
            checks.append(
                Check(
                    "base",
                    "WARN",
                    f"base {remote.base!r} not resolved yet",
                    f"Run `slurmdeck remote connect {remote.name}`.",
                )
            )
        elif observation.base_writable:
            checks.append(Check("base", "OK", remote.resolved_base))
        else:
            checks.append(Check("base", "FAILED", f"{remote.resolved_base} not writable", "Check permissions."))

        if remote.cluster is None:
            checks.append(
                Check(
                    "cluster profile",
                    "WARN",
                    "no explicit cluster capability policy is configured",
                    f"Save one with `slurmdeck remote profile set {remote.name} --file PROFILE.yaml`.",
                )
            )
        else:
            contract = ClusterCapabilityService().resolve(remote.cluster, observation)
            details = []
            if contract.missing:
                details.append("missing: " + ", ".join(contract.missing))
            if contract.conflicts:
                details.append("conflicts: " + "; ".join(contract.conflicts))
            checks.append(
                Check(
                    "cluster profile",
                    "OK" if contract.complete else "WARN",
                    "complete" if contract.complete else " | ".join(details),
                    "Edit the profile explicitly; Doctor never applies recommendations.",
                )
            )
        return checks

    def _database_check(self) -> Check:
        assert self._ctx.project is not None
        path = self._ctx.project.paths.db_path
        if not path.is_file():
            return Check("database", "FAILED", f"{path} does not exist", "Run a mutating project command to create it.")
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return Check("database", "FAILED", str(exc), "Restore or recreate the project database.")
        if version != DB_SCHEMA_VERSION:
            return Check(
                "database",
                "FAILED",
                f"schema version {version}, expected {DB_SCHEMA_VERSION}",
                "Use a fresh 0.1.0 project state directory.",
            )
        return Check("database", "OK", str(path))
