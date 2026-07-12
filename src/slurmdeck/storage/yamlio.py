"""Strict YAML loading into pydantic models with actionable error messages."""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from slurmdeck.errors import UserError
from slurmdeck.storage.permissions import PRIVATE_FILE_MODE, ensure_private_directory, restrict_file_if_present

ModelT = TypeVar("ModelT", bound=BaseModel)


def format_validation_error(source: str, error: ValidationError) -> str:
    lines = [f"{source}: {error.error_count()} validation error(s)"]
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        lines.append(f"  {location}: {item['msg']}")
    return "\n".join(lines)


def load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UserError(f"File not found: {path}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UserError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise UserError(f"{path}: top level must be a mapping")
    return data


def load_yaml_model(path: Path, model_cls: type[ModelT], *, hint: str | None = None) -> ModelT:
    data = load_yaml_mapping(path)
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise UserError(format_validation_error(str(path), exc), hint=hint) from exc


def dump_yaml_model(path: Path, model: BaseModel) -> None:
    """Durably write a YAML model as private local state."""
    dump_yaml_model_atomic(path, model)


def dump_yaml_model_atomic(path: Path, model: BaseModel) -> None:
    """Durably replace a YAML model only after serialization and validation succeeded."""
    payload = model.model_dump(mode="json", exclude_none=True)
    dump_yaml_mapping_atomic(path, payload)


def dump_yaml_mapping_atomic(path: Path, payload: dict[str, object]) -> None:
    """Durably replace a YAML mapping without exposing partial or public state."""
    encoded = yaml.safe_dump(payload, sort_keys=False).encode()
    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        restrict_file_if_present(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(OSError):
            temporary.unlink()
