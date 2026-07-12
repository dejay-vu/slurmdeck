"""Slurm resource requests, with override merging used by submit and retry."""

from __future__ import annotations

from slurmdeck.models.common import StrictModel


class Resources(StrictModel):
    time: str = "12:00:00"
    cpus: int = 1
    mem: str = "8G"
    gres: str | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    constraint: str | None = None
    max_parallel: int | None = None

    def merged(self, overrides: ResourceOverrides) -> Resources:
        """Apply non-None override fields on top of these resources."""
        data = self.model_dump()
        data.update({key: value for key, value in overrides.model_dump().items() if value is not None})
        return Resources.model_validate(data)


class ResourceOverrides(StrictModel):
    """Per-submit CLI overrides; ``None`` means "keep the project default"."""

    time: str | None = None
    cpus: int | None = None
    mem: str | None = None
    gres: str | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    constraint: str | None = None
    max_parallel: int | None = None
