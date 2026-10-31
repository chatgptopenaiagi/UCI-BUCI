"""Behavioral tests with real subprocesses, temporary projects and corruptions."""

from __future__ import annotations
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from uci_buci.memory import ProjectMemory, MemoryError, atomic_write, write_json, validate_config
from uci_buci import objectives, discovery, engineering_cycle, verification, recovery, preservation, project_registry
from uci_buci.cli import main


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.memory = ProjectMemory(self.root)
        self.state = self.memory.initialize()

    def config(self, agent=None, tests=None, limits=None):
        value = validate_config({"agent_command": agent or ["{python}", "-c", "from pathlib import Path; Path('result.txt').write_text('verified')"],
                                 "verification_commands": tests if tests is not None else [["{python}", "-c", "from pathlib import Path; assert Path('result.txt').read_text() == 'verified'"]],
                                 "limits": limits or {}})
        write_json(self.memory.directory / "config.json", value)
        return value

    def objective(self, title="Implement a verified result", **kwargs):
        value = objectives.add(self.state, title, **kwargs)
        self.memory.save(self.state)
        return value


class MemoryTests(ProjectCase):
    def test_initialization_creates_all_memory_files(self):
        for name in ("ENGINEERING-STATE.json", "OBJECTIVES.md", "SESSION-HANDOFF.md", "FAILURE-HISTORY.md", "TEST-HISTORY.md", "ARTIFACT-MANIFEST.json", "config.json"):
            self.assertTrue((self.memory.directory / name).is_file(), name)
        self.assertEqual(1, self.state["schema_version"])

    def test_reinitialization_does_not_destroy_progress_or_config(self):
        self.objective()
        configured = self.config()
        result = self.memory.initialize({"agent_command": ["different"]})
        self.assertEqual(1, len(result["objectives"]))
        self.assertEqual(configured, self.memory.config())

    def test_atomic_replace_failure_preserves_previous_generation(self):
        target = self.root / "owned.json"
        atomic_write(target, "old")
        with patch("uci_buci.memory.os.replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                atomic_write(target, "new")
        self.assertEqual("old", target.read_text())
        self.assertEqual([], list(self.root.glob(".write-*")))

    def test_invalid_serialization_does_not_replace_state(self):
        original = self.memory.path.read_bytes()
        self.state["invalid"] = float("nan")
        with self.assertRaises(ValueError):
            self.memory.save(self.state)
        self.assertEqual(original, self.memory.path.read_bytes())

    def test_backup_retains_previous_valid_generation(self):
        old = self.memory.load()
        self.objective()
        backup = json.loads(self.memory.path.with_suffix(".json.bak").read_text())
        self.assertEqual(old["generation"], backup["generation"])

    def test_corrupted_json_requires_explicit_recovery(self):
        self.objective()
        self.memory.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(MemoryError):
            self.memory.load()
        result = recovery.resume(self.memory, restore_backup=True)
        self.assertGreater(result["generation"], 1)
        self.assertTrue(list(self.memory.directory.glob("damaged-*.json")))

    def test_corrupt_backup_is_never_restored(self):
        self.memory.path.with_suffix(".json.bak").write_text("{}")
        original = self.memory.path.read_bytes()
        with self.assertRaises(MemoryError):
            recovery.resume(self.memory, restore_backup=True)
        self.assertEqual(original, self.memory.path.read_bytes())

    def test_missing_state_does_not_silently_reinitialize(self):
        self.memory.path.unlink()
        with self.assertRaises(MemoryError):
            self.memory.load()

    def test_projections_are_rebuilt_from_canonical_state(self):
        objective = self.objective()
        (self.memory.directory / "OBJECTIVES.md").write_text("stale")
        recovery.resume(self.memory)
        self.assertIn(objective["id"], (self.memory.directory / "OBJECTIVES.md").read_text())

    def test_exclusive_lock_rejects_second_process(self):
        package_root = str(Path(__file__).resolve().parents[1])
        code = "import sys; sys.path.insert(0,sys.argv[1]); from uci_buci.memory import ProjectMemory; m=ProjectMemory(sys.argv[2]);\nwith m.lock(): print('acquired')"
        with self.memory.lock():
            result = subprocess.run([sys.executable, "-c", code, package_root, str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Project is busy", result.stderr)
        with self.memory.lock():
            pass

    def test_config_rejects_invalid_limits_and_shell_strings(self):
        for value in ({"limits": {"max_cycles": 0}}, {"limits": {"max_seconds": float("inf")}}, {"limits": {"max_cycles": 1.5}}, {"limits": {"retry_attempts": -1}}, {"limits": {"unknown": 1}}, {"agent_command": "python foo.py"}, {"verification_commands": [["script.cmd"]]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(value)


class DiscoveryTests(ProjectCase):
    def test_languages_build_tools_and_tests(self):
        for name in ("main.py", "main.cpp", "main.rs", "Main.java", "main.js", "main.ts", "main.php", "Cargo.toml", "CMakeLists.txt", "test_math.py"):
            (self.root / name).write_text("fixture")
        result = discovery.discover(self.root)
        self.assertTrue({"Python", "C++", "Rust", "Java", "JavaScript", "TypeScript", "PHP"}.issubset(result["languages"]))
        self.assertEqual(2, len(result["build_tools"]))
        self.assertEqual(["test_math.py"], result["test_files"])

    def test_excluded_directories_and_secrets_are_not_scanned(self):
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "ignored.py").write_text("private")
        (self.root / ".env").write_text("secret")
        (self.root / "credential.key").write_text("secret")
        self.assertEqual([], list(discovery.files(self.root)))

    def test_fingerprint_detects_content_and_name_changes(self):
        original = discovery.fingerprint(self.root)
        path = self.root / "a.txt"
        path.write_text("one")
        first = discovery.fingerprint(self.root)
        path.write_text("two")
        second = discovery.fingerprint(self.root)
        path.rename(self.root / "b.txt")
        self.assertEqual(4, len({original, first, second, discovery.fingerprint(self.root)}))

    def test_file_and_byte_budgets_fail_closed(self):
        (self.root / "a").write_text("1234")
        (self.root / "b").write_text("1234")
        with self.assertRaises(ValueError):
            list(discovery.files(self.root, 1))
        with self.assertRaises(ValueError):
            discovery.fingerprint(self.root, max_bytes=1)


class ObjectiveTests(ProjectCase):
    def test_priority_and_dependencies_select_achievable_work(self):
        low = objectives.add(self.state, "foundation", priority=10)
        objectives.add(self.state, "dependent", priority=100, depends=[low["id"]])
        independent = objectives.add(self.state, "independent", priority=50)
        self.assertEqual(independent["id"], objectives.select(self.state)["id"])
        independent["status"] = "done"
        self.assertEqual(low["id"], objectives.select(self.state)["id"])
        low["status"] = "done"
        self.assertEqual("dependent", objectives.select(self.state)["title"])

    def test_invalid_dependencies_and_priority_rejected(self):
        for kwargs in ({"depends": ["missing"]}, {"priority": 101}, {"kind": "unknown"}):
            with self.assertRaises(ValueError):
                objectives.add(self.state, "objective", **kwargs)

    def test_blocked_and_cancelled_objectives_not_selected(self):
        value = self.objective()
        for status in ("blocked", "cancelled", "done", "running"):
            value["status"] = status
            self.assertIsNone(objectives.select(self.state))

    def test_explicit_requeue_resets_failure_budget(self):
        value = self.objective()
        value.update(status="blocked", attempts=2)
        self.state.update(consecutive_failures=2, no_progress_count=2)
        objectives.update(self.state, value["id"], "pending", 90)
        self.assertEqual((0, 90), (value["attempts"], value["priority"]))
        self.assertEqual(0, self.state["consecutive_failures"])


class ProcessTests(ProjectCase):
    def test_success_and_output_capture(self):
        result = verification.run([sys.executable, "-c", "print('actual-result')"], self.root, 5)
        self.assertTrue(verification.passed(result))
        self.assertEqual("actual-result", result["stdout"].strip())

    def test_failure_exit_code_and_stderr(self):
        result = verification.run([sys.executable, "-c", "import sys; print('broken', file=sys.stderr); sys.exit(7)"], self.root, 5)
        self.assertEqual(7, result["exit_code"])
        self.assertIn("broken", result["stderr"])

    def test_timeout_terminates_owned_process(self):
        result = verification.run([sys.executable, "-c", "import time; time.sleep(60)"], self.root, 0.2)
        self.assertTrue(result["timed_out"])
        self.assertLess(result["duration_seconds"], 10)

    def test_output_cap_terminates_and_bounds_retention(self):
        result = verification.run([sys.executable, "-c", "import os;\nwhile True: os.write(1,b'x'*4096)"], self.root, 5, 8192)
        self.assertTrue(result["output_limited"])
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 8192)

    def test_missing_executable_records_failure(self):
        result = verification.run([str(self.root / "missing-executable")], self.root, 1)
        self.assertFalse(verification.passed(result))
        self.assertTrue(result["stderr"])

    def test_stdin_delivers_task_and_closes(self):
        result = verification.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], self.root, 5, stdin="scoped task")
        self.assertEqual("scoped task", result["stdout"].strip())

    def test_expansion_preserves_source_braces(self):
        result = verification.expand(["{python}", "-c", "print({'x': 1})", "{project}"], self.root)
        self.assertEqual("print({'x': 1})", result[2])
        self.assertEqual(sys.executable, result[0])

    def test_test_history_records_real_failure(self):
        config = self.config(tests=[["{python}", "-c", "raise AssertionError('regression')"]])
        result = verification.verify(self.memory, self.state, config["verification_commands"], config)
        self.assertFalse(result["passed"])
        self.assertIn("AssertionError", self.memory.load()["verifications"][-1]["results"][0]["stderr"])
        self.assertIn("FAIL", (self.memory.directory / "TEST-HISTORY.md").read_text())

    def test_empty_verification_never_passes(self):
        result = verification.verify(self.memory, self.state, [], self.memory.config())
        self.assertFalse(result["passed"])


class CycleTests(ProjectCase):
    def test_task_preparation_never_executes_agent(self):
        objective = self.objective()
        self.config()
        result = engineering_cycle.cycle(self.memory)
        self.assertFalse(result["executed"])
        self.assertFalse((self.root / "result.txt").exists())
        self.assertIn("Build, Understand, Correct, Improve", result["task"])
        self.assertEqual("pending", self.memory.load()["objectives"][0]["status"])
        self.assertTrue(Path(result["task_file"]).is_file())

    def test_successful_real_task_execution_and_buci_transitions(self):
        self.objective()
        self.config()
        result = engineering_cycle.cycle(self.memory, True, 1)
        state = self.memory.load()
        self.assertEqual(1, len(result["completed"]))
        self.assertEqual("done", state["objectives"][0]["status"])
        self.assertEqual("succeeded", state["runs"][0]["status"])
        self.assertTrue(state["verifications"][0]["passed"])
        self.assertEqual(set(engineering_cycle.STAGES) - {"DEBUG"}, {x["stage"] for x in state["events"]})
        self.assertEqual({"BUILD", "UNDERSTAND", "CORRECT", "IMPROVE"}, {x["buci"] for x in state["events"]})
        self.assertEqual(1, len(state["checkpoints"]))

    def test_agent_failure_and_retry_limit(self):
        self.objective()
        self.config(agent=["{python}", "-c", "raise SystemExit(3)"])
        result = engineering_cycle.cycle(self.memory, True)
        state = self.memory.load()
        self.assertEqual(2, len(result["attempts"]))
        self.assertEqual("blocked", state["objectives"][0]["status"])
        self.assertEqual(["failed", "failed"], [x["status"] for x in state["runs"]])
        self.assertEqual([], state["verifications"])
        self.assertEqual("consecutive_failures", result["stop_reason"])

    def test_failed_verification_does_not_complete_objective(self):
        self.objective()
        self.config(tests=[["{python}", "-c", "raise SystemExit(1)"]])
        engineering_cycle.cycle(self.memory, True, 1)
        state = self.memory.load()
        self.assertEqual("pending", state["objectives"][0]["status"])
        self.assertEqual("verification_failed", state["runs"][0]["reason"])

    def test_no_source_change_detects_unproductive_loop(self):
        self.objective()
        self.config(agent=["{python}", "-c", "print('done')"], tests=[["{python}", "-c", "pass"]],
                    limits={"consecutive_failures": 10, "retry_attempts": 5, "no_progress_limit": 1})
        result = engineering_cycle.cycle(self.memory, True)
        self.assertEqual(1, len(result["attempts"]))
        self.assertEqual("no_progress", result["stop_reason"])
        self.assertEqual("no_source_change", self.memory.load()["runs"][0]["reason"])

    def test_audit_accepts_unchanged_source_with_verification(self):
        self.objective(kind="audit")
        self.config(agent=["{python}", "-c", "print('inspected')"], tests=[["{python}", "-c", "pass"]])
        self.assertEqual(1, len(engineering_cycle.cycle(self.memory, True, 1)["completed"]))

    def test_flaky_agent_recovers_on_bounded_retry(self):
        self.objective()
        agent = ["{python}", "-c", "from pathlib import Path; import sys; p=Path('attempt'); old=p.exists(); p.write_text('attempted'); Path('result.txt').write_text('verified'); sys.exit(0 if old else 1)"]
        self.config(agent=agent)
        result = engineering_cycle.cycle(self.memory, True)
        self.assertEqual(2, len(result["attempts"]))
        self.assertEqual(1, len(result["completed"]))
        self.assertEqual(0, self.memory.load()["consecutive_failures"])

    def test_cycle_count_limits_even_when_more_work_exists(self):
        self.objective(kind="audit")
        objectives.add(self.state, "second audit", kind="audit")
        self.memory.save(self.state)
        self.config(agent=["{python}", "-c", "pass"], tests=[["{python}", "-c", "pass"]], limits={"max_cycles": 1})
        self.assertEqual(1, len(engineering_cycle.cycle(self.memory, True, 20)["attempts"]))
        self.assertEqual(1, sum(x["status"] == "pending" for x in self.memory.load()["objectives"]))

    def test_duration_budget_bounds_agent(self):
        self.objective()
        self.config(agent=["{python}", "-c", "import time; time.sleep(60)"], limits={"max_seconds": 1})
        result = engineering_cycle.cycle(self.memory, True, max_seconds=0.2)
        self.assertTrue(self.memory.load()["runs"][0]["agent"]["timed_out"])
        self.assertEqual("time_limit", result["stop_reason"])

    def test_configuration_required_before_execution(self):
        self.objective()
        with self.assertRaises(ValueError):
            engineering_cycle.cycle(self.memory, True)
        self.assertEqual([], self.memory.load()["runs"])

    def test_objective_specific_verification_is_enforced(self):
        self.objective(verification=[["{python}", "-c", "raise SystemExit(2)"]])
        self.config()
        engineering_cycle.cycle(self.memory, True, 1)
        self.assertFalse(self.memory.load()["verifications"][0]["passed"])

    def test_unexpected_controller_error_preserves_unfinished_run(self):
        self.objective()
        self.config()
        with patch("uci_buci.engineering_cycle.discover", side_effect=OSError("interrupted probe")):
            with self.assertRaises(OSError):
                engineering_cycle.cycle(self.memory, True, 1)
        state = self.memory.load()
        self.assertEqual("interrupted", state["runs"][0]["status"])
        self.assertEqual("pending", state["objectives"][0]["status"])


class RecoveryAndArtifactTests(ProjectCase):
    def test_interrupted_session_recovery_is_idempotent(self):
        value = self.objective()
        value["status"] = "running"
        self.state["runs"].append({"id": "run-interrupted", "objective_id": value["id"], "status": "running"})
        self.memory.save(self.state)
        with self.assertRaises(ValueError):
            engineering_cycle.cycle(self.memory)
        result = recovery.resume(self.memory)
        self.assertEqual([value["id"]], result["recovered"])
        self.assertEqual("pending", result["next_objective"]["status"])
        self.assertEqual("interrupted", self.memory.load()["runs"][0]["status"])
        self.assertEqual([], recovery.resume(self.memory)["recovered"])

    def test_multiple_projects_keep_independent_queues(self):
        second = self.root / "second"
        second.mkdir()
        other = ProjectMemory(second)
        other.initialize()
        self.objective()
        registry = self.root / "registry.json"
        project_registry.register(registry, "first", self.root)
        project_registry.register(registry, "second", second)
        self.assertEqual(self.root, project_registry.resolve(registry, "first"))
        self.assertEqual([], other.load()["objectives"])
        with self.assertRaises(ValueError):
            project_registry.register(registry, "first", second)

    def test_artifact_hashes_detect_tampering_and_missing_files(self):
        artifact = self.root / "artifact.txt"
        artifact.write_text("release")
        record = preservation.checkpoint(self.memory, self.state, ["artifact.txt"])
        self.assertEqual(preservation.sha256(artifact), record["artifacts"][0]["sha256"])
        self.assertTrue(preservation.verify_artifacts(self.memory, self.state)["passed"])
        artifact.write_text("tampered")
        self.assertFalse(preservation.verify_artifacts(self.memory, self.state)["passed"])
        artifact.unlink()
        self.assertFalse(preservation.verify_artifacts(self.memory, self.state)["passed"])

    def test_artifacts_cannot_escape_project(self):
        with self.assertRaises(ValueError):
            preservation.checkpoint(self.memory, self.state, ["../outside"])
        with self.assertRaises(ValueError):
            preservation.checkpoint(self.memory, self.state, [".uci-buci/config.json"])

    def test_empty_manifest_not_reported_as_verified(self):
        self.assertFalse(preservation.verify_artifacts(self.memory, self.state)["passed"])

    @unittest.skipUnless(shutil.which("git"), "Git unavailable")
    def test_git_checkpoint_prepares_preservation_without_committing(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        result = preservation.checkpoint(self.memory, self.state)
        self.assertTrue(result["git"]["repository"])
        self.assertIsNone(result["git"]["revision"])
        self.assertIn("Commit explicitly", result["git"]["commit_preparation"])
        self.assertTrue((self.memory.directory / "checkpoints" / (result["id"] + ".json")).is_file())


class CliTests(ProjectCase):
    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--project", str(self.root), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_cli_status_has_official_identity(self):
        code, out, err = self.cli("status")
        self.assertEqual(0, code)
        value = json.loads(out)
        self.assertEqual("Universal Continuous Intelligence", value["identity"]["UCI"])
        self.assertEqual(["BUILD", "UNDERSTAND", "CORRECT", "IMPROVE"], value["identity"]["BUCI"])

    def test_cli_add_list_prepare_and_resume(self):
        self.assertEqual(0, self.cli("objectives", "add", "--title", "Improve project", "--priority", "90")[0])
        self.assertEqual(1, len(json.loads(self.cli("objectives")[1])))
        self.assertFalse(json.loads(self.cli("cycle")[1])["executed"])
        self.assertIsNotNone(json.loads(self.cli("resume")[1])["next_objective"])

    def test_cli_verify_returns_failure_exit(self):
        code, out, err = self.cli("verify")
        self.assertEqual(1, code)
        self.assertFalse(json.loads(out)["passed"])

    def test_cli_configuration_error_is_json_and_nonzero(self):
        code, out, err = self.cli("cycle", "--execute")
        self.assertEqual(0, code)  # No objectives means no execution.
        self.objective()
        code, out, err = self.cli("cycle", "--execute")
        self.assertEqual(2, code)
        self.assertIn("requires", json.loads(err)["error"])

    def test_cli_discovery_and_checkpoint(self):
        (self.root / "main.py").write_text("pass")
        self.assertIn("Python", json.loads(self.cli("discover")[1])["languages"])
        self.assertTrue(json.loads(self.cli("checkpoint")[1])["source_fingerprint"])


if __name__ == "__main__":
    unittest.main()
