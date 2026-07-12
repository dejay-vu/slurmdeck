"""Typed values returned by TUI creation forms."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from slurmdeck.models.cluster import ClusterProfile
from slurmdeck.models.env import EnvWaitPolicy
from slurmdeck.models.remote import HostKeyPolicy
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec


@dataclass(frozen=True)
class RemoteDraft:
    name: str
    method: str
    destination: str
    base: str
    host_key_policy: HostKeyPolicy
    use: bool


@dataclass(frozen=True)
class ProfileDraft:
    remote_name: str
    profile: ClusterProfile
    diff: str


@dataclass(frozen=True)
class NewRunDraft:
    command: CommandTemplateSpec
    sweep_file: Path | None
    name: str | None
    overrides: ResourceOverrides
    env_wait: EnvWaitPolicy
    submit: bool
