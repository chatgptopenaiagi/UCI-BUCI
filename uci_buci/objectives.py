"""Stable priority order with explicit dependency and verification contracts."""

from __future__ import annotations
import uuid
from .memory import now, validate_command


def add(state: dict, title: str, priority: int = 50, depends: list[str] | None = None,
        verification: list[list[str]] | None = None, kind: str = "change") -> dict:
    if not title.strip() or "\n" in title or not 0 <= priority <= 100 or kind not in {"change", "audit"}:
        raise ValueError("Use a one-line title, priority 0..100, and kind change or audit.")
    known = {x["id"] for x in state["objectives"]}
    if not set(depends or []).issubset(known):
        raise ValueError("Every dependency must name an existing objective.")
    for command in verification or []:
        validate_command(command)
    objective = {"id": "obj-" + uuid.uuid4().hex[:10], "title": title.strip(), "priority": priority,
                 "status": "pending", "depends_on": depends or [], "verification_commands": verification or [],
                 "kind": kind, "attempts": 0, "created_at": now()}
    state["objectives"].append(objective)
    return objective


def select(state: dict) -> dict | None:
    completed = {x["id"] for x in state["objectives"] if x["status"] == "done"}
    available = [x for x in state["objectives"] if x["status"] == "pending" and set(x["depends_on"]).issubset(completed)]
    return min(available, key=lambda x: (-x["priority"], x["created_at"], x["id"]), default=None)


def update(state: dict, identity: str, status: str | None = None, priority: int | None = None) -> dict:
    objective = next((x for x in state["objectives"] if x["id"] == identity), None)
    if objective is None:
        raise ValueError("Unknown objective.")
    if status:
        if status not in {"pending", "blocked", "cancelled"} or objective["status"] == "running":
            raise ValueError("Only idle objectives can be set pending, blocked or cancelled.")
        objective["status"] = status
        if status == "pending":
            objective["attempts"] = 0
            state["consecutive_failures"] = 0
            state["no_progress_count"] = 0
    if priority is not None:
        if not 0 <= priority <= 100:
            raise ValueError("Priority must be 0..100.")
        objective["priority"] = priority
    return objective
