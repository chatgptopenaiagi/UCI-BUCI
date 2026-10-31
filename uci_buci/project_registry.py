"""An explicitly located registry; each project's state remains independent."""

from __future__ import annotations
import json
from pathlib import Path
from .memory import ProjectMemory, write_json


def register(registry: Path, name: str, project: Path) -> dict:
    project = project.resolve()
    ProjectMemory(project).load()
    if not name.strip():
        raise ValueError("Project name is required.")
    # The registry itself uses a separate OS lock, never a project lock.
    registry = registry.resolve()
    registry.parent.mkdir(parents=True, exist_ok=True)
    lock = ProjectMemory(registry.parent)
    # Use a dedicated directory to keep registry ownership distinct from project ownership.
    lock.directory = registry.parent / ("." + registry.name + ".lock")
    with lock.lock():
        value = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {"schema_version": 1, "projects": {}}
        if name in value["projects"] and Path(value["projects"][name]).resolve() != project:
            raise ValueError("Registry name already refers to a different project.")
        value["projects"][name] = str(project)
        write_json(registry, value)
    return value


def resolve(registry: Path, name: str) -> Path:
    value = json.loads(registry.read_text(encoding="utf-8"))
    if name not in value["projects"]:
        raise ValueError("Project is not registered.")
    return Path(value["projects"][name]).resolve()
