"""Bounded subprocesses with capped captured output and process-tree termination."""

from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from .memory import now, validate_command


def expand(command: list[str], root: Path, task_file: Path | None = None) -> list[str]:
    # Replace only known literal tokens; JSON braces inside Python/JS remain intact.
    replacements = {"{python}": sys.executable, "{project}": str(root), "{task_file}": str(task_file or "")}
    result = []
    for argument in validate_command(command):
        for token, value in replacements.items():
            argument = argument.replace(token, value)
        result.append(argument)
    return result


def terminate(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # Absolute Windows tool path; /T kills only this owned process and children.
        tool = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(tool), "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run(command: list[str], root: Path, timeout: float, max_output_bytes: int = 262144, stdin: str = "") -> dict:
    started = time.monotonic()
    result = {"command": command, "at": now(), "exit_code": None, "timed_out": False,
              "output_limited": False, "stdout": "", "stderr": "", "duration_seconds": 0.0}
    if timeout <= 0:
        result["timed_out"] = True
        return result
    try:
        validate_command(command)
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **options)
    except (OSError, ValueError) as exc:
        result["stderr"] = str(exc)
        return result
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()
    guard = threading.Lock()
    total = 0

    def drain(stream, name):
        nonlocal total
        try:
            while True:
                block = os.read(stream.fileno(), 4096)
                if not block:
                    break
                with guard:
                    remaining = max(0, max_output_bytes - total)
                    buffers[name].extend(block[:remaining])
                    total += len(block)
                    if total > max_output_bytes:
                        exceeded.set()
        except (OSError, ValueError):
            pass

    def feed():
        try:
            process.stdin.write(stdin.encode("utf-8"))
            process.stdin.close()
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True), threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
    for thread in readers:
        thread.start()
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        while process.poll() is None or any(thread.is_alive() for thread in readers):
            if exceeded.is_set() or time.monotonic() - started >= timeout:
                result["output_limited"] = exceeded.is_set()
                result["timed_out"] = not exceeded.is_set()
                terminate(process)
                break
            time.sleep(0.01)
        process.wait(timeout=10)
    except BaseException:
        terminate(process)
        process.wait(timeout=10)
        raise
    finally:
        for thread in readers:
            thread.join(timeout=2)
        writer.join(timeout=1)
        for stream in (process.stdout, process.stderr, process.stdin):
            stream.close()
    result.update({key: bytes(value).decode("utf-8", errors="replace") for key, value in buffers.items()})
    result["exit_code"] = process.returncode
    result["duration_seconds"] = round(time.monotonic() - started, 4)
    return result


def passed(result: dict) -> bool:
    return result["exit_code"] == 0 and not result["timed_out"] and not result["output_limited"]


def verify(memory, state: dict, commands: list[list[str]], config: dict, deadline: float | None = None) -> dict:
    record = {"id": "verify-" + uuid.uuid4().hex[:10], "at": now(), "passed": False, "results": []}
    limits = config["limits"]
    if deadline is None:
        deadline = time.monotonic() + limits["max_seconds"]
    for command in commands:
        remaining = deadline - time.monotonic()
        outcome = run(expand(command, memory.root), memory.root, min(limits["command_seconds"], remaining), limits["max_output_bytes"])
        record["results"].append(outcome)
        if not passed(outcome):
            break
    record["passed"] = bool(commands) and len(record["results"]) == len(commands) and all(passed(x) for x in record["results"])
    if not commands:
        record["reason"] = "No verification commands configured; success cannot be established."
    state["verifications"].append(record)
    memory.save(state)
    return record
