"""Read-only Git evidence and explicit artifact manifests; no automatic push."""

from __future__ import annotations
import hashlib
from pathlib import Path
import shutil
import uuid
from .memory import now
from .verification import run, passed
from .discovery import fingerprint


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def scoped_artifact(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or ".uci-buci" in path.relative_to(root).parts:
        raise ValueError("Artifacts must be existing files inside the project, outside controller memory.")
    return path


def git_state(root: Path) -> dict:
    git = shutil.which("git")
    if not git:
        return {"available": False}
    probe = run([git, "rev-parse", "--show-toplevel"], root, 5)
    if not passed(probe):
        return {"available": True, "repository": False}
    # Never checkpoint an enclosing, unrelated repository as this project.
    if Path(probe["stdout"].strip()).resolve() != root:
        return {"available": True, "repository": False, "reason": "Project is inside another repository."}
    revision = run([git, "rev-parse", "HEAD"], root, 5)
    status = run([git, "status", "--porcelain=v1", "--untracked-files=normal"], root, 5)
    return {"available": True, "repository": True, "revision": revision["stdout"].strip() if passed(revision) else None,
            "dirty": bool(status["stdout"].strip()) if passed(status) else None,
            "status": status["stdout"], "status_verified": passed(status),
            "commit_preparation": ["Review git diff and untracked files", "Stage only reviewed project files", "Commit explicitly", "Push only to an authorized remote"]}


def checkpoint(memory, state: dict, artifacts: list[str] | None = None) -> dict:
    manifest = []
    for name in artifacts or []:
        path = scoped_artifact(memory.root, name)
        manifest.append({"path": path.relative_to(memory.root).as_posix(), "size": path.stat().st_size, "sha256": sha256(path)})
    state["artifacts"] = manifest or state["artifacts"]
    config = memory.config()
    record = {"id": "checkpoint-" + uuid.uuid4().hex[:10], "at": now(),
              "source_fingerprint": fingerprint(memory.root, config["limits"]["max_files"], config["limits"]["max_source_bytes"]),
              "git": git_state(memory.root), "artifacts": list(state["artifacts"]),
              "verification_id": state["verifications"][-1]["id"] if state["verifications"] else None}
    state["checkpoints"].append(record)
    memory.save(state)
    # Immutable per-checkpoint copy, in addition to canonical state history.
    from .memory import write_json
    write_json(memory.directory / "checkpoints" / (record["id"] + ".json"), record)
    return record


def verify_artifacts(memory, state: dict) -> dict:
    results = []
    for artifact in state["artifacts"]:
        try:
            path = scoped_artifact(memory.root, artifact["path"])
            valid = path.stat().st_size == artifact["size"] and sha256(path) == artifact["sha256"]
        except (ValueError, OSError):
            valid = False
        results.append({"path": artifact["path"], "passed": valid})
    return {"passed": bool(results) and all(x["passed"] for x in results), "results": results}
