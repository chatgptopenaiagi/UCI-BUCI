"""Machine-readable CLI: successful commands emit JSON; failures exit nonzero."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from . import __version__
from .memory import ProjectMemory, write_json, validate_config
from . import discovery, objectives, engineering_cycle, verification, preservation, recovery, reporting, project_registry


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="UCI-BUCI: Universal Continuous Intelligence — Build, Understand, Correct, Improve")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--project", default=".", help="Managed directory; place before the subcommand")
    root.add_argument("--registry", type=Path, help="Explicit multiproject registry path")
    root.add_argument("--name", help="Registered project name")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize persistent project memory")
    init.add_argument("--codex", action="store_true", help="Configure local codex exec; does not run it")
    init.add_argument("--agent-command", type=json.loads, help="JSON argv array; task is also sent on stdin")
    init.add_argument("--test-command", action="append", type=json.loads, default=[], help="Repeatable JSON argv array")
    commands.add_parser("discover", help="Inspect languages, build manifests and tests")
    commands.add_parser("status", help="Show engineering state and recent evidence")
    obj = commands.add_parser("objectives", help="List, add or update objectives")
    obj.add_argument("action", choices=("list", "add", "update"), nargs="?", default="list")
    obj.add_argument("--title")
    obj.add_argument("--id")
    obj.add_argument("--priority", type=int)
    obj.add_argument("--depends", action="append", default=[])
    obj.add_argument("--test-command", action="append", type=json.loads, default=[])
    obj.add_argument("--kind", choices=("change", "audit"), default="change")
    obj.add_argument("--state", choices=("pending", "blocked", "cancelled"))
    cyc = commands.add_parser("cycle", help="Prepare a task; --execute opts into bounded execution")
    cyc.add_argument("--execute", action="store_true")
    cyc.add_argument("--max-cycles", type=int)
    cyc.add_argument("--max-seconds", type=float)
    ver = commands.add_parser("verify", help="Run configured tests or check recorded artifact hashes")
    ver.add_argument("--artifacts", action="store_true")
    cp = commands.add_parser("checkpoint", help="Record source revision, fingerprint and artifact metadata")
    cp.add_argument("--artifact", action="append", default=[])
    res = commands.add_parser("resume", help="Recover interrupted state and the next objective")
    res.add_argument("--restore-backup", action="store_true")
    cfg = commands.add_parser("configure", help="Validate and replace project configuration from a JSON file")
    cfg.add_argument("file", type=Path)
    reg = commands.add_parser("register", help="Register this initialized project")
    reg.add_argument("project_name")
    return root


def dispatch(args) -> tuple[dict | list, int]:
    project = project_registry.resolve(args.registry, args.name) if args.name and args.registry else Path(args.project)
    if args.name and not args.registry:
        raise ValueError("--name requires --registry.")
    memory = ProjectMemory(project)
    if args.command == "init":
        agent = args.agent_command or (["codex", "exec", "--sandbox", "workspace-write", "-"] if args.codex else [])
        return memory.initialize({"agent_command": agent, "verification_commands": args.test_command}), 0
    if args.command == "resume":
        return recovery.resume(memory, args.restore_backup), 0
    if args.command == "cycle":
        value = engineering_cycle.cycle(memory, args.execute, args.max_cycles, args.max_seconds)
        failure = bool(value.get("attempts")) and not value.get("completed")
        if value.get("stop_reason") in {"consecutive_failures", "no_progress", "retry_limit", "time_limit"}:
            failure = True
        return value, 1 if failure else 0
    if args.command == "register":
        if not args.registry:
            raise ValueError("register requires --registry.")
        return project_registry.register(args.registry, args.project_name, memory.root), 0
    with memory.lock():
        state = memory.load()
        if args.command == "configure":
            config = validate_config(json.loads(args.file.read_text(encoding="utf-8")))
            write_json(memory.directory / "config.json", config)
            return config, 0
        if args.command == "status":
            return reporting.status(state), 0
        if args.command == "discover":
            state["discovery"] = discovery.discover(memory.root, memory.config()["limits"]["max_files"])
            memory.save(state)
            return state["discovery"], 0
        if args.command == "objectives":
            if args.action == "add":
                if not args.title:
                    raise ValueError("objectives add requires --title.")
                value = objectives.add(state, args.title, args.priority if args.priority is not None else 50, args.depends, args.test_command, args.kind)
            elif args.action == "update":
                value = objectives.update(state, args.id, args.state, args.priority)
            else:
                return state["objectives"], 0
            memory.save(state)
            return value, 0
        if args.command == "verify":
            value = preservation.verify_artifacts(memory, state) if args.artifacts else verification.verify(memory, state, memory.config()["verification_commands"], memory.config())
            return value, 0 if value["passed"] else 1
        if args.command == "checkpoint":
            return preservation.checkpoint(memory, state, args.artifact), 0
    raise ValueError("Unknown command.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value, code = dispatch(args)
        print(json.dumps(value, indent=2, ensure_ascii=True))
        return code
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"error":"Interrupted. Inspect changes and use resume before continuing."}', file=sys.stderr)
        return 130
