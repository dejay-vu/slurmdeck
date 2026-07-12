"""Pure run planning: validated inputs to a complete in-memory payload."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.common import safe_name, validate_name
from slurmdeck.models.env import EnvBinding, EnvWaitPolicy
from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import ResourceOverrides, Resources
from slurmdeck.models.run import CommandTemplateSpec, RunManifest, TaskSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.planning.commandline import command_mentions_args, resolve_command
from slurmdeck.planning.placeholders import expand_text, expand_value
from slurmdeck.planning.sweep import TaskDraft, expand_sweep, render_args_from_config
from slurmdeck.rendering import render_template
from slurmdeck.services.context import AppContext
from slurmdeck.services.env_binding import activation_script_for_binding
from slurmdeck.services.snapshots import SnapshotService
from slurmdeck.storage.repos import PlannedTaskRecord, RunRow


@dataclass(frozen=True)
class PlannedTask:
    record: PlannedTaskRecord
    rendered_config: bytes | None


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    manifest: RunManifest
    tasks: tuple[PlannedTask, ...]
    rendered_files: Mapping[str, bytes]


@dataclass(frozen=True)
class _TaskInput:
    """Everything needed to plan one task, from a sweep draft or a retry."""

    task_id: str
    name: str
    params: dict[str, Any]
    config: dict[str, Any] | None
    config_resolved: bool
    args_template: list[str] | None
    env_template: dict[str, str]
    arg_style: str


class RunPlanner:
    """Resolve a run completely without opening storage or changing the filesystem."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._snapshots = SnapshotService()

    def plan(
        self,
        *,
        command: CommandTemplateSpec,
        sweep: Sweep | None = None,
        sweep_file: str | None = None,
        name: str | None = None,
        overrides: ResourceOverrides | None = None,
        remote: Remote,
        env_binding: EnvBinding | None = None,
    ) -> RunPlan:
        project = self._ctx.require_project()
        if name:
            validate_name(name, what="run name")

        drafts = expand_sweep(sweep) if sweep is not None else [TaskDraft(index=0, name="task")]
        inputs = tuple(
            _TaskInput(
                task_id=self._task_id(draft.index, len(drafts)),
                name=draft.name,
                params=dict(draft.params),
                config=draft.config,
                config_resolved=False,
                args_template=draft.args_template,
                env_template=draft.env_template,
                arg_style=draft.arg_style,
            )
            for draft in drafts
        )
        resources = project.config.resources.merged(overrides or ResourceOverrides())
        self._validate_resources(resources)
        run_id = self._new_run_id(name or (sweep.name if sweep else None) or "run")
        return self._plan_inputs(
            run_id=run_id,
            name=name or run_id,
            remote=remote,
            resources=resources,
            command=command,
            inputs=inputs,
            sweep_file=sweep_file,
            retry_of=None,
            env_binding=env_binding,
        )

    def retry(
        self,
        *,
        source: RunRow,
        records: Mapping[str, PlannedTaskRecord],
        task_ids: Sequence[str],
        remote: Remote,
    ) -> RunPlan:
        if not task_ids:
            raise UserError(f"Run {source.id} has no failed tasks to retry.")
        missing = [task_id for task_id in task_ids if task_id not in records]
        if missing:
            raise UserError(f"Unknown task ids for run {source.id}: {', '.join(missing)}.")

        project = self._ctx.require_project()
        source_run_dir = project.paths.run_dir(source.id)
        inputs: list[_TaskInput] = []
        for task_id in task_ids:
            record = records[task_id]
            config_dict: dict[str, Any] | None = None
            if record.spec.config:
                self._validate_relative_path(record.spec.config)
                config_path = source_run_dir.joinpath(*PurePosixPath(record.spec.config).parts)
                if not config_path.is_file():
                    raise UserError(
                        f"Missing local config for task {task_id}: {config_path}",
                        hint="The source run directory is incomplete; retry from an intact run.",
                    )
                try:
                    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                    raise UserError(f"Invalid local config for task {task_id}: {config_path}.") from exc
                if not isinstance(loaded, dict):
                    raise UserError(f"Invalid local config for task {task_id}: expected a mapping.")
                config_dict = loaded
            inputs.append(
                _TaskInput(
                    task_id=record.spec.task_id,
                    name=record.spec.name,
                    params=dict(record.params),
                    config=config_dict,
                    config_resolved=True,
                    args_template=record.args_template,
                    env_template=record.env_template,
                    arg_style=record.arg_style,
                )
            )

        self._validate_resources(source.resources)
        new_id = self._new_run_id(f"{source.name}-retry")
        return self._plan_inputs(
            run_id=new_id,
            name=new_id,
            remote=remote,
            resources=source.resources,
            command=source.command,
            inputs=tuple(inputs),
            sweep_file=source.sweep_file,
            retry_of=source.id,
            env_binding=(
                source.env_binding.model_copy(update={"wait_policy": EnvWaitPolicy.READY, "build_job_id": ""})
                if source.env_binding is not None
                else None
            ),
        )

    def _plan_inputs(
        self,
        *,
        run_id: str,
        name: str,
        remote: Remote,
        resources: Resources,
        command: CommandTemplateSpec,
        inputs: tuple[_TaskInput, ...],
        sweep_file: str | None,
        retry_of: str | None,
        env_binding: EnvBinding | None,
    ) -> RunPlan:
        project = self._ctx.require_project()
        layout = self._ctx.layout(remote)
        remote_root = layout.run_root(run_id)
        snapshot_hash, _files = self._snapshots.compute(project.paths.root, project.config.sync)

        activation = ""
        if project.config.env is None and env_binding is not None:
            raise UserError("An environment binding was supplied for a project with no environment configuration.")
        if project.config.env is not None and env_binding is None:
            raise UserError(
                "The run needs an exact environment binding.",
                hint="Resolve the environment before planning the run, or run `slurmdeck submit`.",
            )
        if project.config.env is not None and env_binding is not None:
            activation = activation_script_for_binding(project.config.env, remote.cluster, env_binding)

        planned_tasks = tuple(
            self._plan_task(
                index=index,
                item=item,
                run_id=run_id,
                remote_root=remote_root,
                command=command,
            )
            for index, item in enumerate(inputs)
        )
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest = RunManifest(
            project_id=project.config.project_id,
            project_display_name=project.config.display_name,
            run_id=run_id,
            name=name,
            created_at=created_at,
            remote=remote.name,
            remote_root=remote_root,
            snapshot_hash=snapshot_hash,
            env_binding=env_binding,
            env_dependency_state=(
                "waiting"
                if env_binding is not None and env_binding.wait_policy is EnvWaitPolicy.AFTEROK
                else "ready"
                if env_binding is not None
                else ""
            ),
            env_dependency_reason=(
                f"Waiting for environment {env_binding.env_id} build {env_binding.build_job_id}"
                if env_binding is not None and env_binding.wait_policy is EnvWaitPolicy.AFTEROK
                else ""
            ),
            resources=resources,
            command=command,
            sweep_file=sweep_file,
            retry_of=retry_of,
            task_count=len(planned_tasks),
        )

        task_jsonl = "".join(task.record.spec.model_dump_json(exclude_none=True) + "\n" for task in planned_tasks)
        rendered_files: dict[str, bytes] = {
            protocol.TASKS_FILE: task_jsonl.encode(),
            protocol.AGENT_FILE: protocol.agent_source().encode(),
            protocol.ACTIVATION_FILE: activation.encode(),
            protocol.RUN_MANIFEST_FILE: (manifest.model_dump_json(indent=2) + "\n").encode(),
            protocol.SBATCH_FILE: render_template(
                "array.sbatch.j2",
                job_name=run_id,
                resources=resources,
                task_count=len(planned_tasks),
                run_root=remote_root,
                code_dir=layout.snapshot_code_dir(snapshot_hash),
            ).encode(),
            f"{protocol.CONFIGS_DIR}/.keep": b"",
            f"{protocol.LOGS_DIR}/.keep": b"",
            f"{protocol.RESULTS_DIR}/.keep": b"",
        }
        for task in planned_tasks:
            if task.record.spec.config is not None and task.rendered_config is not None:
                rendered_files[task.record.spec.config] = task.rendered_config
        for relative_path in rendered_files:
            self._validate_relative_path(relative_path)

        return RunPlan(
            run_id=run_id,
            manifest=manifest,
            tasks=planned_tasks,
            rendered_files=MappingProxyType(rendered_files),
        )

    def _plan_task(
        self,
        *,
        index: int,
        item: _TaskInput,
        run_id: str,
        remote_root: str,
        command: CommandTemplateSpec,
    ) -> PlannedTask:
        result_rel = f"{protocol.RESULTS_DIR}/{item.task_id}"
        base_context: dict[str, Any] = {
            **item.params,
            "index": index,
            "task_id": item.task_id,
            "task_name": item.name,
            "run": run_id,
        }

        config_rel: str | None = None
        config_data: dict[str, Any] | None = None
        rendered_config: bytes | None = None
        if item.config is not None:
            position = f"task {item.task_id} config"
            config_data = (
                item.config if item.config_resolved else expand_value(item.config, base_context, position=position)
            )
            config_rel = f"{protocol.CONFIGS_DIR}/{item.task_id}_{safe_name(item.name)}.yaml"
            rendered_config = yaml.safe_dump(config_data, sort_keys=False).encode()

        full_context = dict(base_context)
        full_context["output"] = f"{remote_root}/{result_rel}"
        if config_rel is not None:
            full_context["config"] = f"{remote_root}/{config_rel}"

        if item.args_template is not None:
            args = [
                expand_text(arg, full_context, position=f"task {item.task_id} args[{position}]")
                for position, arg in enumerate(item.args_template)
            ]
        elif config_data is not None:
            args = render_args_from_config(config_data, item.arg_style)  # type: ignore[arg-type]
        else:
            args = []

        if args and not command_mentions_args(command):
            raise UserError(
                f"The sweep produces task arguments but the command never uses {{args}} "
                f"(task {item.task_id} args: {args[:4]}...).",
                hint="Add {args} to your command, set `arg_style: none`, or reference {config} instead.",
            )

        env = {
            key: expand_text(value, full_context, position=f"task {item.task_id} env[{key}]")
            for key, value in item.env_template.items()
        }
        argv, shell = resolve_command(command, full_context, args=args)
        spec = TaskSpec(
            index=index,
            task_id=item.task_id,
            name=item.name,
            argv=argv,
            shell=shell,
            env=env,
            config=config_rel,
            result_dir=result_rel,
        )
        self._validate_task(spec)
        record = PlannedTaskRecord(
            spec=spec,
            params=item.params,
            args_template=item.args_template,
            env_template=item.env_template,
            arg_style=item.arg_style,
        )
        return PlannedTask(record=record, rendered_config=rendered_config)

    @staticmethod
    def _task_id(index: int, total: int) -> str:
        width = max(3, len(str(max(total - 1, 0))))
        return f"{index:0{width}d}"

    @staticmethod
    def _new_run_id(base: str) -> str:
        stem = safe_name(base, fallback="run")[:39].rstrip("-._") or "run"
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        return f"{stem}-{timestamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _validate_resources(resources: Resources) -> None:
        if resources.cpus <= 0:
            raise UserError("Run resources cpus must be greater than zero.")
        if resources.max_parallel is not None and resources.max_parallel <= 0:
            raise UserError("Run resources max_parallel must be greater than zero.")
        for field in ("time", "mem", "gres", "partition", "account", "qos", "constraint"):
            value = getattr(resources, field)
            if value is None:
                continue
            if field in {"time", "mem"} and not value.strip():
                raise UserError(f"Run resources {field} must not be empty.")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise UserError(f"Run resources {field} contains a control character.")

    @classmethod
    def _validate_task(cls, spec: TaskSpec) -> None:
        validate_name(spec.task_id, what="task id")
        validate_name(spec.name, what="task name")
        cls._validate_relative_path(spec.result_dir)
        if spec.config is not None:
            cls._validate_relative_path(spec.config)
        if spec.argv is not None and any("\0" in value for value in spec.argv):
            raise UserError(f"Task {spec.task_id} command contains a NUL character.")
        if spec.shell is not None and "\0" in spec.shell:
            raise UserError(f"Task {spec.task_id} command contains a NUL character.")
        for key, value in spec.env.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
                raise UserError(f"Task {spec.task_id} has an invalid environment variable name: {key!r}.")
            if "\0" in value:
                raise UserError(f"Task {spec.task_id} environment variable {key!r} contains a NUL character.")

    @staticmethod
    def _validate_relative_path(relative_path: str) -> None:
        path = PurePosixPath(relative_path)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise UserError(f"Unsafe rendered run path: {relative_path!r}.")
