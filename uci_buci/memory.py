"""Atomic canonical JSON, recoverable projections, and OS-owned project locks."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


class MemoryError(ValueError):
    """Invalid or unavailable project memory (never silently reset)."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


DEFAULT_CONFIG = {
    "agent_command": [],
    "verification_commands": [],
    "limits": {
        "max_cycles": 3,
        "max_seconds": 300,
        "command_seconds": 120,
        "retry_attempts": 1,
        "consecutive_failures": 2,
        "no_progress_limit": 2,
        "max_output_bytes": 262144,
        "max_files": 5000,
        "max_source_bytes": 67108864,
    },
}


def validate_command(command: object) -> list[str]:
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x and "\0" not in x for x in command):
        raise MemoryError("Commands must be nonempty JSON arrays of nonempty strings.")
    if command[0].lower().endswith((".bat", ".cmd")):
        raise MemoryError("Use a native executable, not a batch wrapper.")
    return command


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise MemoryError("Configuration must be a JSON object.")
    merged = copy.deepcopy(DEFAULT_CONFIG)
    merged.update({k: v for k, v in config.items() if k != "limits"})
    if not isinstance(config.get("limits", {}), dict):
        raise MemoryError("limits must be an object.")
    merged["limits"].update(config.get("limits", {}))
    for key, value in merged["limits"].items():
        if key not in DEFAULT_CONFIG["limits"] or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise MemoryError(f"Invalid limit: {key}")
        if value < (0 if key == "retry_attempts" else 1):
            raise MemoryError(f"Limit {key} is outside its valid range.")
        if key not in {"max_seconds", "command_seconds"} and not isinstance(value, int):
            raise MemoryError(f"Limit {key} must be an integer.")
    if merged["agent_command"]:
        validate_command(merged["agent_command"])
    if not isinstance(merged["verification_commands"], list):
        raise MemoryError("verification_commands must be an array of commands.")
    for command in merged["verification_commands"]:
        validate_command(command)
    return merged


class ProjectMemory:
    def __init__(self, project: str | Path):
        self.root = Path(project).resolve()
        if not self.root.is_dir():
            raise MemoryError(f"Project directory does not exist: {self.root}")
        self.directory = self.root / ".uci-buci"
        if self.directory.is_symlink() or (self.directory.exists() and self.directory.resolve().parent != self.root):
            raise MemoryError("Project memory must be a real directory inside the project.")
        self.path = self.directory / "ENGINEERING-STATE.json"

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.directory.mkdir(exist_ok=True)
        with (self.directory / "controller.lock").open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MemoryError("Project is busy: another controller owns its lock.") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def initialize(self, config: dict | None = None) -> dict:
        with self.lock():
            if self.path.exists() or self.path.with_suffix(".json.bak").exists():
                return self.load()
            config = validate_config(config or {})
            write_json(self.directory / "config.json", config)
            state = {"schema_version": 1, "generation": 0, "created_at": now(),
                     "project_name": self.root.name, "phase": "idle", "objectives": [],
                     "runs": [], "verifications": [], "checkpoints": [], "events": [],
                     "consecutive_failures": 0, "no_progress_count": 0, "artifacts": []}
            self.save(state)
            return state

    def load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("schema_version") != 1 or not all(isinstance(state.get(k), list) for k in ("objectives", "runs", "verifications", "checkpoints", "events", "artifacts")):
                raise ValueError("Unsupported or invalid state schema")
            return state
        except (OSError, ValueError, AttributeError) as exc:
            raise MemoryError("Cannot read canonical state. Use resume --restore-backup for a validated previous generation.") from exc

    def config(self) -> dict:
        try:
            return validate_config(json.loads((self.directory / "config.json").read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise MemoryError(f"Invalid project configuration: {exc}") from exc

    def save(self, state: dict) -> None:
        state["generation"] += 1
        state["updated_at"] = now()
        # Validate serialization before replacing any prior data.
        payload = json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if self.path.exists():
            self.load()  # Never overwrite a corrupted canonical generation.
            atomic_write(self.path.with_suffix(".json.bak"), self.path.read_text(encoding="utf-8"))
        atomic_write(self.path, payload)
        self.render(state)

    def render(self, state: dict) -> None:
        # JSON is authoritative. Interrupted projections are regenerated by resume.
        def rows(items: list, formatter) -> str:
            return "\n".join(formatter(x) for x in items) or "No records."
        objectives = rows(state["objectives"], lambda x: f"- [{ 'x' if x['status'] == 'done' else ' ' }] {x['id']} | priority {x['priority']} | {x['status']} | {x['title']}")
        atomic_write(self.directory / "OBJECTIVES.md", "# Objectives\n\n" + objectives + "\n")
        atomic_write(self.directory / "TEST-HISTORY.md", "# Test history\n\n" + rows(state["verifications"], lambda x: f"- {x['at']} | {x['id']} | {'PASS' if x['passed'] else 'FAIL'} | {len(x['results'])} commands") + "\n")
        atomic_write(self.directory / "FAILURE-HISTORY.md", "# Failure history\n\n" + rows([x for x in state["runs"] if x["status"] != "succeeded"], lambda x: f"- {x['id']} | {x['objective_id']} | {x['status']} | {x.get('reason', '')}") + "\n")
        pending = [x["id"] + ": " + x["title"] for x in state["objectives"] if x["status"] != "done"]
        atomic_write(self.directory / "SESSION-HANDOFF.md", f"# Session handoff\n\nGeneration: {state['generation']}\nPhase: {state['phase']}\n\nRead ENGINEERING-STATE.json, config.json, OBJECTIVES.md, TEST-HISTORY.md and FAILURE-HISTORY.md.\nRun `uci-buci --project . resume` before execution.\nBUCI: BUILD → UNDERSTAND → CORRECT → IMPROVE → REPEAT.\n\nUnfinished:\n" + "\n".join("- " + x for x in pending) + "\n")
        write_json(self.directory / "ARTIFACT-MANIFEST.json", {"schema_version": 1, "generation": state["generation"], "artifacts": state["artifacts"]})
