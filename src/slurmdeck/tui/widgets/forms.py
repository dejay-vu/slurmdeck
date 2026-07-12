"""Typed TUI forms for remotes, profiles, and new runs."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import ClassVar, cast

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, TextArea

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import ClusterProfile
from slurmdeck.models.env import EnvWaitPolicy
from slurmdeck.models.remote import HostKeyPolicy
from slurmdeck.models.resources import ResourceOverrides, Resources
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services.remotes import RemoteService
from slurmdeck.storage.yamlio import format_validation_error
from slurmdeck.tui.drafts import NewRunDraft, ProfileDraft, RemoteDraft


def _optional(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _commands(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _tri_value(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"


def _tri_parse(value: object) -> bool | None:
    return None if value == "unknown" else value == "yes"


class RemoteFormModal(ModalScreen[RemoteDraft | None]):
    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="form-dialog"):
            yield Label("Add remote", classes="form-title")
            with VerticalScroll(id="remote-fields"):
                yield Label("Name")
                yield Input(placeholder="cluster", id="remote-name")
                yield Label("Connection method")
                yield Select(
                    [("Host (user@login.example.com)", "host"), ("SSH config alias", "ssh_alias")],
                    value="host",
                    allow_blank=False,
                    id="remote-method",
                )
                yield Label("Destination")
                yield Input(placeholder="user@login.example.com or ssh alias", id="remote-destination")
                yield Label("Remote base")
                yield Input(placeholder="$WORK/slurmdeck", id="remote-base")
                yield Label("Host-key policy")
                yield Select(
                    [
                        ("Use OpenSSH config (recommended)", HostKeyPolicy.INHERIT.value),
                        ("Strict (known hosts only)", HostKeyPolicy.STRICT.value),
                        ("Accept new keys", HostKeyPolicy.ACCEPT_NEW.value),
                    ],
                    value=HostKeyPolicy.INHERIT.value,
                    allow_blank=False,
                    id="remote-host-key-policy",
                )
                yield Checkbox("Use immediately", value=True, id="remote-use")
            with Horizontal(classes="form-buttons"):
                yield Button("Add", variant="primary", id="remote-save")
                yield Button("Cancel", id="remote-cancel")

    def on_mount(self) -> None:
        self.query_one("#remote-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        name = self.query_one("#remote-name", Input).value.strip()
        method = cast(str, self.query_one("#remote-method", Select).value)
        destination = self.query_one("#remote-destination", Input).value.strip()
        base = self.query_one("#remote-base", Input).value.strip()
        host_key_policy = HostKeyPolicy(cast(str, self.query_one("#remote-host-key-policy", Select).value))
        if not name or not destination or not base:
            self.notify("Name, destination, and remote base are required.", severity="error")
            return
        self.dismiss(
            RemoteDraft(
                name=name,
                method=method,
                destination=destination,
                base=base,
                host_key_policy=host_key_policy,
                use=self.query_one("#remote-use", Checkbox).value,
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._save() if event.button.id == "remote-save" else self.dismiss(None)


class ProfileModal(ModalScreen[ProfileDraft | None]):
    """Complete ClusterProfile form with import, diff preview, and explicit save."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, remote_name: str, current: ClusterProfile | None) -> None:
        super().__init__()
        self.remote_name = remote_name
        self.current = current

    @property
    def remote_service(self) -> RemoteService:
        from slurmdeck.tui.app import SlurmDeckApp

        return RemoteService(cast("SlurmDeckApp", self.app).ctx)

    def compose(self) -> ComposeResult:
        with Vertical(id="form-dialog"):
            yield Label(f"Cluster profile · {self.remote_name}", classes="form-title")
            with Horizontal(classes="form-row"):
                yield Input(placeholder="/path/to/profile.yaml", id="profile-import-path")
                yield Button("Import YAML", id="profile-import")
                yield Button("Preview diff", id="profile-preview")
            with VerticalScroll(id="profile-fields"):
                yield Label("Executors", classes="form-section")
                yield Checkbox("Allow Slurm builds", id="profile-allow-slurm")
                yield Checkbox("Allow login-node builds", id="profile-allow-login")
                yield Label("Default executor")
                yield Select(
                    [("Unspecified", ""), ("Slurm", "slurm"), ("Login", "login")],
                    value="",
                    allow_blank=False,
                    id="profile-default-executor",
                )
                yield Label("Login build policy")
                yield Select(
                    [("Unspecified", ""), ("Forbidden", "forbidden"), ("Allowed", "allowed")],
                    value="",
                    allow_blank=False,
                    id="profile-login-policy",
                )
                yield Label("Shared filesystem (login → compute)")
                yield Select(
                    [("Unknown", "unknown"), ("Yes", "yes"), ("No", "no")],
                    value="unknown",
                    allow_blank=False,
                    id="profile-shared-fs",
                )

                yield Label("Modules and conda", classes="form-section")
                yield Label("Module initialization")
                yield Select(
                    [("Unspecified", ""), ("None", "none"), ("Source file", "source"), ("Commands", "commands")],
                    value="",
                    allow_blank=False,
                    id="profile-module-strategy",
                )
                yield Input(placeholder="source file, e.g. /etc/profile.d/modules.sh", id="profile-module-source")
                yield Input(placeholder="commands separated by ;", id="profile-module-commands")
                yield Input(placeholder="conda executable", id="profile-conda-executable")
                yield Input(placeholder="conda modules, comma-separated", id="profile-conda-modules")

                yield Label("Network", classes="form-section")
                yield Select(
                    [("Unspecified", ""), ("Full", "full"), ("Restricted", "restricted"), ("None", "none")],
                    value="",
                    allow_blank=False,
                    id="profile-compute-access",
                )
                yield Select(
                    [("Unspecified", ""), ("Direct", "direct"), ("Mirrors", "mirrors"), ("None", "none")],
                    value="",
                    allow_blank=False,
                    id="profile-channel-access",
                )
                yield Input(placeholder="channel mirrors, comma-separated", id="profile-mirrors")

                yield Label("Slurm defaults", classes="form-section")
                yield Input(placeholder="partition", id="profile-partition")
                yield Input(placeholder="account", id="profile-account")
                yield Input(placeholder="qos", id="profile-qos")
                yield Input(placeholder="constraint", id="profile-constraint")
                yield Label("afterok dependency")
                yield Select(
                    [("Unknown", "unknown"), ("Supported", "yes"), ("Unsupported", "no")],
                    value="unknown",
                    allow_blank=False,
                    id="profile-afterok",
                )
                yield Label("Invalid dependency termination")
                yield Select(
                    [
                        ("Unspecified", ""),
                        ("Per job", "per_job"),
                        ("Site wide", "site_wide"),
                        ("Unsupported", "unsupported"),
                    ],
                    value="",
                    allow_blank=False,
                    id="profile-invalid-dependency",
                )

                yield Label("Platform", classes="form-section")
                yield Input(placeholder="system, e.g. Linux", id="profile-system")
                yield Input(placeholder="machine, e.g. x86_64", id="profile-machine")
                yield Input(placeholder="conda subdir, e.g. linux-64", id="profile-conda-subdir")

                yield Label("Diff preview", classes="form-section")
                yield TextArea("", read_only=True, show_line_numbers=False, id="profile-diff")
            with Horizontal(classes="form-buttons"):
                yield Button("Save profile", variant="primary", id="profile-save")
                yield Button("Cancel", id="profile-cancel")

    def on_mount(self) -> None:
        self._load(self.current or ClusterProfile())
        self.query_one("#profile-import-path", Input).focus()

    def _input(self, selector: str) -> str:
        return self.query_one(selector, Input).value

    def _select(self, selector: str) -> object:
        return self.query_one(selector, Select).value

    def _load(self, profile: ClusterProfile) -> None:
        allowed = set(profile.allowed_build_executors)
        self.query_one("#profile-allow-slurm", Checkbox).value = "slurm" in allowed
        self.query_one("#profile-allow-login", Checkbox).value = "login" in allowed
        self.query_one("#profile-default-executor", Select).value = profile.default_build_executor or ""
        self.query_one("#profile-login-policy", Select).value = profile.login_build_policy or ""
        self.query_one("#profile-shared-fs", Select).value = _tri_value(profile.shared_filesystem.login_to_compute)
        self.query_one("#profile-module-strategy", Select).value = profile.module_initialization.strategy or ""
        self.query_one("#profile-module-source", Input).value = profile.module_initialization.source or ""
        self.query_one("#profile-module-commands", Input).value = "; ".join(profile.module_initialization.commands)
        self.query_one("#profile-conda-executable", Input).value = profile.conda.executable or ""
        self.query_one("#profile-conda-modules", Input).value = ", ".join(profile.conda.modules)
        self.query_one("#profile-compute-access", Select).value = profile.network.compute_access or ""
        self.query_one("#profile-channel-access", Select).value = profile.network.channel_access or ""
        self.query_one("#profile-mirrors", Input).value = ", ".join(profile.network.mirrors)
        self.query_one("#profile-partition", Input).value = profile.slurm.partition or ""
        self.query_one("#profile-account", Input).value = profile.slurm.account or ""
        self.query_one("#profile-qos", Input).value = profile.slurm.qos or ""
        self.query_one("#profile-constraint", Input).value = profile.slurm.constraint or ""
        self.query_one("#profile-afterok", Select).value = _tri_value(profile.slurm.afterok_dependency)
        self.query_one("#profile-invalid-dependency", Select).value = profile.slurm.kill_invalid_dependency or ""
        self.query_one("#profile-system", Input).value = profile.platform.system or ""
        self.query_one("#profile-machine", Input).value = profile.platform.machine or ""
        self.query_one("#profile-conda-subdir", Input).value = profile.platform.conda_subdir or ""
        self._preview(profile)

    def _collect(self) -> ClusterProfile | None:
        allowed = []
        if self.query_one("#profile-allow-slurm", Checkbox).value:
            allowed.append("slurm")
        if self.query_one("#profile-allow-login", Checkbox).value:
            allowed.append("login")
        data = {
            "schema_version": 1,
            "allowed_build_executors": allowed,
            "default_build_executor": _optional(str(self._select("#profile-default-executor"))),
            "login_build_policy": _optional(str(self._select("#profile-login-policy"))),
            "shared_filesystem": {"login_to_compute": _tri_parse(self._select("#profile-shared-fs"))},
            "module_initialization": {
                "strategy": _optional(str(self._select("#profile-module-strategy"))),
                "source": _optional(self._input("#profile-module-source")),
                "commands": _commands(self._input("#profile-module-commands")),
            },
            "conda": {
                "executable": _optional(self._input("#profile-conda-executable")),
                "modules": _csv(self._input("#profile-conda-modules")),
            },
            "network": {
                "compute_access": _optional(str(self._select("#profile-compute-access"))),
                "channel_access": _optional(str(self._select("#profile-channel-access"))),
                "mirrors": _csv(self._input("#profile-mirrors")),
            },
            "slurm": {
                "partition": _optional(self._input("#profile-partition")),
                "account": _optional(self._input("#profile-account")),
                "qos": _optional(self._input("#profile-qos")),
                "constraint": _optional(self._input("#profile-constraint")),
                "afterok_dependency": _tri_parse(self._select("#profile-afterok")),
                "kill_invalid_dependency": _optional(str(self._select("#profile-invalid-dependency"))),
            },
            "platform": {
                "system": _optional(self._input("#profile-system")),
                "machine": _optional(self._input("#profile-machine")),
                "conda_subdir": _optional(self._input("#profile-conda-subdir")),
            },
        }
        try:
            return ClusterProfile.model_validate(data)
        except ValidationError as exc:
            self.notify(format_validation_error("cluster profile", exc), severity="error", timeout=10)
            return None

    def _preview(self, profile: ClusterProfile) -> str:
        diff = self.remote_service.profile_diff(self.remote_name, profile)
        self.query_one("#profile-diff", TextArea).load_text(diff or "No changes.")
        return diff

    def _import(self) -> None:
        path = Path(self._input("#profile-import-path")).expanduser()
        try:
            profile = self.remote_service.validate_profile_file(path)
        except (OSError, UserError) as exc:
            self.notify(str(exc), severity="error", timeout=10)
            return
        self._load(profile)

    def _save(self) -> None:
        profile = self._collect()
        if profile is not None:
            self.dismiss(ProfileDraft(self.remote_name, profile, self._preview(profile)))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "profile-import":
            self._import()
        elif event.button.id == "profile-preview":
            if (profile := self._collect()) is not None:
                self._preview(profile)
        elif event.button.id == "profile-save":
            self._save()
        else:
            self.dismiss(None)


class NewRunModal(ModalScreen[NewRunDraft | None]):
    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, *, afterok_eligible: bool, resources: Resources, project_root: Path) -> None:
        super().__init__()
        self.afterok_eligible = afterok_eligible
        self.resources = resources
        self.project_root = project_root

    def compose(self) -> ComposeResult:
        wait_options = [("Wait until environment is READY", EnvWaitPolicy.READY.value)]
        if self.afterok_eligible:
            wait_options.append(("Submit after environment build succeeds", EnvWaitPolicy.AFTEROK.value))
        with Vertical(id="form-dialog"):
            yield Label("New run", classes="form-title")
            with VerticalScroll(id="run-fields"):
                yield Label("Command")
                yield Input(placeholder="python train.py --config {config}", id="run-command")
                yield Label("Existing sweep YAML (optional)")
                yield Input(placeholder="sweep.yaml", id="run-sweep")
                yield Input(placeholder="run name (optional)", id="run-name")
                yield Label("Resources", classes="form-section")
                yield Input(value=self.resources.time, placeholder="time", id="run-time")
                yield Input(value=str(self.resources.cpus), placeholder="cpus", type="integer", id="run-cpus")
                yield Input(value=self.resources.mem, placeholder="memory", id="run-mem")
                yield Input(value=self.resources.gres or "", placeholder="gres, e.g. gpu:1", id="run-gres")
                yield Input(value=self.resources.partition or "", placeholder="partition", id="run-partition")
                yield Input(value=self.resources.account or "", placeholder="account", id="run-account")
                yield Input(value=self.resources.qos or "", placeholder="qos", id="run-qos")
                yield Input(value=self.resources.constraint or "", placeholder="constraint", id="run-constraint")
                yield Input(
                    value=str(self.resources.max_parallel) if self.resources.max_parallel is not None else "",
                    placeholder="max parallel",
                    type="integer",
                    id="run-max-parallel",
                )
                yield Label("Environment wait policy", classes="form-section")
                yield Select(
                    wait_options,
                    value=EnvWaitPolicy.READY.value,
                    allow_blank=False,
                    id="run-env-wait",
                )
                if not self.afterok_eligible:
                    yield Label(
                        "afterok appears only for an active Slurm environment build "
                        "with safe invalid-dependency policy.",
                        classes="form-note",
                    )
                yield Checkbox("Submit immediately", value=True, id="run-submit")
            with Horizontal(classes="form-buttons"):
                yield Button("Create run", variant="primary", id="run-save")
                yield Button("Cancel", id="run-cancel")

    def on_mount(self) -> None:
        self.query_one("#run-command", Input).focus()

    def _integer(self, selector: str) -> int | None:
        value = self.query_one(selector, Input).value.strip()
        return int(value) if value else None

    def _save(self) -> None:
        command_text = self.query_one("#run-command", Input).value.strip()
        if not command_text:
            self.notify("Command is required.", severity="error")
            return
        try:
            argv = shlex.split(command_text)
            command = CommandTemplateSpec(argv=argv)
            sweep_text = self.query_one("#run-sweep", Input).value.strip()
            sweep_file = Path(sweep_text).expanduser() if sweep_text else None
            if sweep_file is not None and not sweep_file.is_absolute():
                sweep_file = self.project_root / sweep_file
            if sweep_file is not None and not sweep_file.is_file():
                raise UserError(f"Sweep file does not exist: {sweep_file}")
            overrides = ResourceOverrides(
                time=_optional(self.query_one("#run-time", Input).value),
                cpus=self._integer("#run-cpus"),
                mem=_optional(self.query_one("#run-mem", Input).value),
                gres=_optional(self.query_one("#run-gres", Input).value),
                partition=_optional(self.query_one("#run-partition", Input).value),
                account=_optional(self.query_one("#run-account", Input).value),
                qos=_optional(self.query_one("#run-qos", Input).value),
                constraint=_optional(self.query_one("#run-constraint", Input).value),
                max_parallel=self._integer("#run-max-parallel"),
            )
            wait_policy = EnvWaitPolicy(str(self.query_one("#run-env-wait", Select).value))
        except (UserError, ValidationError, ValueError) as exc:
            self.notify(str(exc), severity="error", timeout=10)
            return
        self.dismiss(
            NewRunDraft(
                command=command,
                sweep_file=sweep_file,
                name=_optional(self.query_one("#run-name", Input).value),
                overrides=overrides,
                env_wait=wait_policy,
                submit=self.query_one("#run-submit", Checkbox).value,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._save() if event.button.id == "run-save" else self.dismiss(None)
