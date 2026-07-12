"""Remote cluster definition (stored as one YAML file per remote)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator

from slurmdeck.models.cluster import ClusterProfile
from slurmdeck.models.common import NameStr, StrictModel


class HostKeyPolicy(StrEnum):
    """How SlurmDeck applies OpenSSH host-key verification policy."""

    INHERIT = "inherit"
    STRICT = "strict"
    ACCEPT_NEW = "accept-new"


class Remote(StrictModel):
    """An SSH-reachable cluster login host plus a base directory for slurmdeck state.

    Exactly one of ``host`` (``user@login.example.com``) or ``ssh_alias`` (an
    alias from the user's ``~/.ssh/config``) must be set. Authentication is
    always delegated to the user's own SSH setup; slurmdeck never stores
    credentials.
    """

    name: NameStr
    host: str | None = None
    ssh_alias: str | None = None
    base: str
    host_key_policy: HostKeyPolicy = HostKeyPolicy.INHERIT
    #: Cache written by `slurmdeck remote connect`: ``base`` with remote-side
    #: ``~``/``$VAR`` expansion applied. Planning is pure-local and needs it.
    resolved_base: str | None = None
    cluster: ClusterProfile | None = None

    @model_validator(mode="after")
    def _exactly_one_destination(self) -> Remote:
        if bool(self.host) == bool(self.ssh_alias):
            raise ValueError("set exactly one of 'host' or 'ssh_alias'")
        if not self.base.strip():
            raise ValueError("'base' must be a non-empty remote path")
        return self

    @property
    def destination(self) -> str:
        """The ssh destination argument."""
        dest = self.ssh_alias or self.host
        assert dest is not None
        return dest
