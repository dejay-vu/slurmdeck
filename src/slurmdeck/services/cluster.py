"""Read-only cluster observation and explicit profile contract resolution."""

from __future__ import annotations

from pydantic import ValidationError

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import (
    BuildExecutor,
    ChannelAccess,
    ClusterObservation,
    ClusterProfile,
    ComputeNetworkAccess,
    EffectiveClusterContract,
    InvalidDependencyPolicy,
    LoginBuildPolicy,
)
from slurmdeck.models.remote import Remote
from slurmdeck.operations import OperationPhase
from slurmdeck.structured_errors import StructuredError
from slurmdeck.transport import Transport, TransportError

_CLUSTER_PROBE = r"""
import json, os, platform, shutil, subprocess, sys, time

base = sys.argv[2] if len(sys.argv) > 2 else ""
errors = []
tools = {}
for name in ("sbatch", "squeue", "sacct", "scancel", "sinfo"):
    path = shutil.which(name) or ""
    version = ""
    if path:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5, check=False)
            lines = (result.stdout or result.stderr).strip().splitlines()
            version = lines[0] if lines else ""
        except Exception as exc:
            errors.append("%s version probe failed: %s" % (name, exc))
    tools[name] = {"available": bool(path), "path": path, "version": version}

afterok_dependency_supported = None
kill_invalid_dependency_supported = None
if tools["sbatch"]["available"]:
    try:
        result = subprocess.run(
            [tools["sbatch"]["path"], "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            help_text = result.stdout + result.stderr
            afterok_dependency_supported = "--dependency" in help_text
            kill_invalid_dependency_supported = "--kill-on-invalid-dep" in help_text
        else:
            errors.append("sbatch help probe exited with code %s" % result.returncode)
    except Exception as exc:
        errors.append("sbatch dependency probe failed: %s" % exc)

partitions = []
default_partition = None
if tools["sinfo"]["available"]:
    try:
        result = subprocess.run(
            [tools["sinfo"]["path"], "-h", "-o", "%P|%l"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.strip().split("|", 1)
                if len(fields) != 2:
                    continue
                raw_name, max_time = fields
                is_default = raw_name.endswith("*")
                name = raw_name.rstrip("*")
                if not name:
                    continue
                partitions.append({"name": name, "is_default": is_default, "max_time": max_time})
                if is_default:
                    default_partition = name
        else:
            errors.append("sinfo exited with code %s: %s" % (result.returncode, result.stderr.strip()))
    except Exception as exc:
        errors.append("sinfo partition probe failed: %s" % exc)

system = platform.system()
machine = platform.machine()
subdirs = {
    ("Linux", "x86_64"): "linux-64",
    ("Linux", "aarch64"): "linux-aarch64",
    ("Darwin", "x86_64"): "osx-64",
    ("Darwin", "arm64"): "osx-arm64",
}
payload = {
    "schema_version": 1,
    "observed_at": time.time(),
    "python_version": ".".join(str(part) for part in sys.version_info[:3]),
    "tools": tools,
    "base_writable": (os.path.isdir(base) and os.access(base, os.W_OK)) if base else None,
    "default_partition": default_partition,
    "partitions": partitions,
    "system": system,
    "machine": machine,
    "conda_subdir": subdirs.get((system, machine)),
    "module_available": bool(shutil.which("modulecmd")),
    "conda_path": shutil.which("conda"),
    "shared_path_visible": None,
    "afterok_dependency_supported": afterok_dependency_supported,
    "kill_invalid_dependency_supported": kill_invalid_dependency_supported,
    "errors": errors,
}
print("SLURMDECK_JSON\t" + json.dumps(payload, sort_keys=True))
"""


class ClusterCapabilityService:
    def observe(self, transport: Transport, remote: Remote) -> ClusterObservation:
        raw = transport.exec_json(
            _CLUSTER_PROBE,
            ["cluster-observe", remote.resolved_base or ""],
            timeout=60,
        )
        try:
            return ClusterObservation.model_validate(raw)
        except ValidationError as exc:
            raise TransportError(
                "Remote cluster observation returned an invalid contract.",
                underlying_cause=exc,
            ) from exc

    def resolve(
        self,
        profile: ClusterProfile | None,
        observation: ClusterObservation | None,
        *,
        requested_executor: BuildExecutor | None = None,
    ) -> EffectiveClusterContract:
        if profile is None:
            return EffectiveClusterContract(
                profile_present=False,
                requested_executor=requested_executor,
                complete=False,
                missing=["cluster_profile"],
            )

        missing: list[str] = []
        conflicts: list[str] = []
        warnings: list[str] = []

        if not profile.allowed_build_executors:
            missing.append("allowed_build_executors")
        if profile.default_build_executor is None:
            missing.append("default_build_executor")
        if profile.login_build_policy is None:
            missing.append("login_build_policy")
        if profile.shared_filesystem.login_to_compute is None:
            missing.append("shared_filesystem.login_to_compute")
        elif not profile.shared_filesystem.login_to_compute:
            conflicts.append("SlurmDeck base is not declared visible from login to compute nodes")
        if profile.module_initialization.strategy is None:
            missing.append("module_initialization.strategy")
        if not (profile.conda.executable or profile.conda.modules):
            missing.append("conda.executable_or_modules")
        if profile.network.compute_access is None:
            missing.append("network.compute_access")
        elif profile.network.compute_access is ComputeNetworkAccess.NONE:
            conflicts.append("build executors have no declared network access")
        if profile.network.channel_access is None:
            missing.append("network.channel_access")
        elif profile.network.channel_access is ChannelAccess.NONE:
            conflicts.append("build executors have no declared channel access")
        if profile.platform.system is None:
            missing.append("platform.system")
        if profile.platform.machine is None:
            missing.append("platform.machine")
        if profile.platform.conda_subdir is None:
            missing.append("platform.conda_subdir")
        if profile.slurm.afterok_dependency is None:
            missing.append("slurm.afterok_dependency")
        if profile.slurm.kill_invalid_dependency is None:
            missing.append("slurm.kill_invalid_dependency")

        executor = requested_executor or profile.default_build_executor
        if requested_executor is not None and requested_executor not in profile.allowed_build_executors:
            conflicts.append(f"requested executor {requested_executor.value!r} is not allowed by the profile")
        if executor is BuildExecutor.LOGIN and profile.login_build_policy is not LoginBuildPolicy.ALLOWED:
            conflicts.append("login builds are not explicitly allowed")

        effective_partition = profile.slurm.partition
        if observation is None:
            missing.append("cluster_observation")
        else:
            if observation.base_writable is False:
                conflicts.append("configured SlurmDeck base is not writable")
            if observation.shared_path_visible is False:
                conflicts.append("observed path visibility contradicts the shared filesystem profile")
            if profile.platform.system and profile.platform.system != observation.system:
                conflicts.append(
                    f"platform.system {profile.platform.system!r} does not match observed {observation.system!r}"
                )
            if profile.platform.machine and profile.platform.machine != observation.machine:
                conflicts.append(
                    f"platform.machine {profile.platform.machine!r} does not match observed {observation.machine!r}"
                )
            if (
                profile.platform.conda_subdir
                and observation.conda_subdir
                and profile.platform.conda_subdir != observation.conda_subdir
            ):
                conflicts.append(
                    "platform.conda_subdir "
                    f"{profile.platform.conda_subdir!r} does not match observed {observation.conda_subdir!r}"
                )
            if executor is BuildExecutor.SLURM:
                for tool in ("sbatch", "squeue", "sacct", "scancel", "sinfo"):
                    observed_tool = observation.tools.get(tool)
                    if observed_tool is None or not observed_tool.available:
                        conflicts.append(f"required Slurm tool {tool!r} is unavailable")
                effective_partition = effective_partition or observation.default_partition
                if effective_partition is None:
                    missing.append("slurm.partition_or_observed_default")
                elif observation.partitions and effective_partition not in {
                    partition.name for partition in observation.partitions
                }:
                    conflicts.append(f"Slurm partition {effective_partition!r} was not observed")
            if profile.slurm.afterok_dependency is True and observation.afterok_dependency_supported is False:
                conflicts.append("sbatch does not expose afterok dependency support")
            if (
                profile.slurm.kill_invalid_dependency is InvalidDependencyPolicy.PER_JOB
                and observation.kill_invalid_dependency_supported is False
            ):
                conflicts.append("sbatch does not expose --kill-on-invalid-dep")
            if profile.conda.executable and not profile.conda.modules and not observation.conda_path:
                warnings.append("configured conda executable was not found without module initialization")
            warnings.extend(observation.errors)

        kill_policy = profile.slurm.kill_invalid_dependency
        afterok_eligible = bool(
            executor is BuildExecutor.SLURM
            and profile.slurm.afterok_dependency
            and kill_policy in (InvalidDependencyPolicy.PER_JOB, InvalidDependencyPolicy.SITE_WIDE)
        )
        return EffectiveClusterContract(
            profile_present=True,
            requested_executor=requested_executor,
            executor=executor,
            effective_partition=effective_partition,
            effective_account=profile.slurm.account,
            effective_qos=profile.slurm.qos,
            effective_constraint=profile.slurm.constraint,
            complete=not missing and not conflicts,
            missing=missing,
            conflicts=conflicts,
            warnings=warnings,
            afterok_eligible=afterok_eligible,
            kill_invalid_dependency=kill_policy,
        )

    @staticmethod
    def require_complete(contract: EffectiveClusterContract, *, operation: str) -> None:
        if contract.complete:
            return
        detail_parts = []
        if contract.missing:
            detail_parts.append("Missing: " + ", ".join(contract.missing))
        if contract.conflicts:
            detail_parts.append("Conflicts: " + "; ".join(contract.conflicts))
        raise UserError(
            StructuredError(
                code="cluster_contract_incomplete",
                summary="The cluster profile is not complete enough for this managed operation.",
                detail="\n".join(detail_parts),
                operation=operation,
                phase=OperationPhase.VALIDATE,
                retryable=False,
                remediation="Save an explicit cluster profile with `slurmdeck remote profile set`.",
                context={"missing": contract.missing, "conflicts": contract.conflicts},
            )
        )
