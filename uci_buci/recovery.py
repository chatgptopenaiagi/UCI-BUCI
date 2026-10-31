"""Interrupted work remains unfinished; recovery never invents verification."""

from __future__ import annotations
import json
from .memory import atomic_write, MemoryError, now
from .objectives import select


def resume(memory, restore_backup: bool = False) -> dict:
    with memory.lock():
        if restore_backup:
            backup = memory.path.with_suffix(".json.bak")
            try:
                payload = backup.read_text(encoding="utf-8")
                value = json.loads(payload)
                if value.get("schema_version") != 1 or not all(isinstance(value.get(k), list) for k in ("objectives", "runs", "verifications", "checkpoints", "events", "artifacts")):
                    raise ValueError("Invalid backup schema")
            except (OSError, ValueError, AttributeError) as exc:
                raise MemoryError("A validated backup is unavailable.") from exc
            if memory.path.exists():
                atomic_write(memory.directory / ("damaged-" + now().replace(":", "-") + ".json"), memory.path.read_text(encoding="utf-8"))
            atomic_write(memory.path, payload)
        state = memory.load()
        recovered = []
        for objective in state["objectives"]:
            if objective["status"] == "running":
                objective["status"] = "pending"
                recovered.append(objective["id"])
        for run in state["runs"]:
            if run["status"] == "running":
                run.update(status="interrupted", reason="Recovered interrupted controller; inspect changes and rerun verification.", finished_at=now())
        state["phase"] = "idle"
        memory.save(state)
        return {"recovered": recovered, "next_objective": select(state), "generation": state["generation"],
                "instruction": "Inspect unfinished changes. Recheck config and verification before cycle --execute. Account/usage limits require waiting or an authorized external session; they are never bypassed."}
