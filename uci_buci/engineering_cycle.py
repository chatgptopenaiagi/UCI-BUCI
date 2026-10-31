"""A bounded controller for verified increments, not unbounded code generation."""

from __future__ import annotations
from pathlib import Path
import time
import uuid
from .memory import atomic_write, now
from .objectives import select
from .discovery import discover, fingerprint
from .verification import run, expand, passed, verify
from .preservation import checkpoint

STAGES = ("DISCOVER", "ANALYZE", "DESIGN", "CODE", "EXECUTE", "TEST", "DEBUG", "VERIFY", "OPTIMIZE", "PRESERVE", "REPEAT")
BUCI_STAGE = {"DISCOVER": "UNDERSTAND", "ANALYZE": "UNDERSTAND", "DESIGN": "BUILD", "CODE": "BUILD", "EXECUTE": "BUILD", "TEST": "CORRECT", "DEBUG": "CORRECT", "VERIFY": "CORRECT", "OPTIMIZE": "IMPROVE", "PRESERVE": "IMPROVE", "REPEAT": "IMPROVE"}


def event(memory, state: dict, stage: str, objective_id: str) -> None:
    state["phase"] = stage.lower()
    state["events"].append({"at": now(), "stage": stage, "buci": BUCI_STAGE[stage], "objective_id": objective_id})
    memory.save(state)


def task_text(memory, state: dict, objective: dict, config: dict) -> str:
    failures = [x for x in state["runs"] if x["objective_id"] == objective["id"] and x["status"] != "succeeded"][-3:]
    return f"""# UCI-BUCI scoped engineering task

UCI = Universal Continuous Intelligence.
BUCI = Build, Understand, Correct, Improve.
Every verified improvement begins the next evolution.

Authorized root: {memory.root}
Objective: {objective['id']} — {objective['title']}
Acceptance kind: {objective['kind']} (change requires a changed source fingerprint).
Dependencies: {objective['depends_on']}
Verification commands (argv): {config['verification_commands'] + objective['verification_commands']}
Controller limits: {config['limits']}
Recent attempt summaries: {[(x['status'], x.get('reason')) for x in failures]}

Read project AGENTS.md and .uci-buci/SESSION-HANDOFF.md. Inspect architecture and existing functionality.
DISCOVER → ANALYZE → DESIGN → CODE → EXECUTE → TEST → DEBUG → VERIFY → OPTIMIZE → PRESERVE → REPEAT.
BUILD → UNDERSTAND → CORRECT → IMPROVE → REPEAT.
Implement only this objective in this project. Add meaningful regressions and run the acceptance commands.
Do not weaken tests to obtain a pass. Do not modify .uci-buci, credentials, global settings or unrelated projects.
Do not launch another controller, commit, push, publish, install global software, or start detached/background work.
Report actual results and remaining limits. Stop after one scoped increment. The controller independently verifies it.
"""


def cycle(memory, execute: bool = False, max_cycles: int | None = None, max_seconds: float | None = None) -> dict:
    with memory.lock():
        state, config = memory.load(), memory.config()
        limits = config["limits"]
        if any(x["status"] == "running" for x in state["runs"]):
            raise ValueError("Interrupted run detected; use resume before another cycle.")
        if max_cycles is not None and max_cycles < 1 or max_seconds is not None and max_seconds <= 0:
            raise ValueError("Requested cycle/time limits must be positive.")
        count = min(max_cycles if max_cycles is not None else limits["max_cycles"], limits["max_cycles"])
        duration = min(max_seconds if max_seconds is not None else limits["max_seconds"], limits["max_seconds"])
        deadline = time.monotonic() + duration
        completed, attempts, reason = [], [], "cycle_limit"
        for _ in range(count):
            if time.monotonic() >= deadline:
                reason = "time_limit"
                break
            if execute and state["consecutive_failures"] >= limits["consecutive_failures"]:
                reason = "consecutive_failures"
                break
            if execute and state["no_progress_count"] >= limits["no_progress_limit"]:
                reason = "no_progress"
                break
            objective = select(state)
            if objective is None:
                reason = "no_achievable_objective"
                break
            commands = config["verification_commands"] + objective["verification_commands"]
            text = task_text(memory, state, objective, config)
            task_file = memory.directory / "tasks" / (objective["id"] + ".md")
            atomic_write(task_file, text)
            if not execute:
                return {"executed": False, "objective": objective, "task_file": str(task_file), "task": text}
            if not config["agent_command"] or not commands:
                raise ValueError("Execution requires an explicit agent_command and at least one verification command in config.json or the objective.")
            if objective["attempts"] > limits["retry_attempts"]:
                objective["status"] = "blocked"
                memory.save(state)
                reason = "retry_limit"
                break
            before = fingerprint(memory.root, limits["max_files"], limits["max_source_bytes"])
            record = {"id": "run-" + uuid.uuid4().hex[:10], "at": now(), "objective_id": objective["id"], "status": "running", "before": before}
            objective["status"] = "running"
            objective["attempts"] += 1
            state["runs"].append(record)
            memory.save(state)
            try:
                event(memory, state, "DISCOVER", objective["id"])
                state["discovery"] = discover(memory.root, limits["max_files"])
                event(memory, state, "ANALYZE", objective["id"])
                event(memory, state, "DESIGN", objective["id"])
                event(memory, state, "CODE", objective["id"])
                agent = run(expand(config["agent_command"], memory.root, task_file), memory.root,
                            min(limits["command_seconds"], deadline - time.monotonic()), limits["max_output_bytes"], text)
                record["agent"] = agent
                event(memory, state, "EXECUTE", objective["id"])
                verification = None
                if passed(agent):
                    event(memory, state, "TEST", objective["id"])
                    verification = verify(memory, state, commands, config, deadline)
                    record["verification_id"] = verification["id"]
                after = fingerprint(memory.root, limits["max_files"], limits["max_source_bytes"])
                record["after"] = after
                changed = before != after
                success = passed(agent) and bool(verification and verification["passed"]) and (changed or objective["kind"] == "audit")
                if success:
                    event(memory, state, "VERIFY", objective["id"])
                    record.update(status="succeeded", reason="Agent and independent verification passed.")
                    objective["status"] = "done"
                    objective["completed_at"] = now()
                    state["consecutive_failures"] = 0
                    state["no_progress_count"] = 0
                    event(memory, state, "OPTIMIZE", objective["id"])
                    completed.append(objective["id"])
                else:
                    event(memory, state, "DEBUG", objective["id"])
                    failure = "agent_failed" if not passed(agent) else "verification_failed" if not verification["passed"] else "no_source_change"
                    record.update(status="failed", reason=failure)
                    objective["status"] = "blocked" if objective["attempts"] > limits["retry_attempts"] else "pending"
                    state["consecutive_failures"] += 1
                    state["no_progress_count"] = state["no_progress_count"] + 1 if not changed else 0
                record["finished_at"] = now()
                event(memory, state, "PRESERVE", objective["id"])
                checkpoint(memory, state)
                event(memory, state, "REPEAT", objective["id"])
            except Exception as exc:
                # Preserve unexpected controller errors without claiming task success.
                record.update(status="interrupted", reason=str(exc), finished_at=now())
                objective["status"] = "pending"
                state["phase"] = "interrupted"
                memory.save(state)
                raise
            attempts.append(record["id"])
        state["phase"] = "idle"
        memory.save(state)
        return {"executed": execute, "completed": completed, "attempts": attempts, "stop_reason": reason,
                "next_objective": select(state)}
