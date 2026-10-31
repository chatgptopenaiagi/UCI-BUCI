"""Bounded inspection; discovery never runs a project's programs."""

from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil

EXCLUDED = {".git", ".uci-buci", ".venv", "venv", "node_modules", "target", "build", "dist", "__pycache__", ".local", ".pytest_cache"}
LANGUAGES = {".py": "Python", ".cpp": "C++", ".cc": "C++", ".h": "C/C++", ".c": "C", ".rs": "Rust", ".java": "Java", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".php": "PHP", ".ps1": "PowerShell", ".go": "Go"}
BUILD_FILES = {"pyproject.toml": "Python packaging", "package.json": "npm", "Cargo.toml": "Cargo", "CMakeLists.txt": "CMake", "pom.xml": "Maven", "build.gradle": "Gradle", "composer.json": "Composer", "Makefile": "Make"}


def files(root: Path, max_files: int = 5000):
    count = 0
    for directory, names, entries in os.walk(root, followlinks=False):
        names[:] = sorted(n for n in names if n not in EXCLUDED and not n.startswith(".venv") and not n.endswith(".egg-info") and not (Path(directory) / n).is_symlink() and not getattr(Path(directory) / n, "is_junction", lambda: False)())
        for name in sorted(entries):
            path = Path(directory) / name
            if path.is_symlink() or name.startswith(".env") or path.suffix.lower() in {".pem", ".key", ".pyc"}:
                continue
            count += 1
            if count > max_files:
                raise ValueError("Project file limit exceeded; narrow the managed project.")
            yield path


def fingerprint(root: Path, max_files=5000, max_bytes=67108864) -> str:
    digest = hashlib.sha256()
    size = 0
    for path in files(root, max_files):
        size += path.stat().st_size
        if size > max_bytes:
            raise ValueError("Project byte limit exceeded; narrow the managed project.")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def discover(root: Path, max_files=5000) -> dict:
    languages, builds, tests, structure = {}, [], [], []
    for path in files(root, max_files):
        relative = path.relative_to(root).as_posix()
        language = LANGUAGES.get(path.suffix.lower())
        if language:
            languages[language] = languages.get(language, 0) + 1
        if path.name in BUILD_FILES:
            builds.append({"path": relative, "tool": BUILD_FILES[path.name]})
        if path.name.startswith("test_") or path.name.endswith(("_test.py", ".test.js", ".test.ts")) or "tests" in path.parts:
            tests.append(relative)
        if len(structure) < 100:
            structure.append(relative)
    return {"languages": languages, "build_tools": builds, "test_files": tests, "structure_sample": structure,
            "executables": {x: shutil.which(x) for x in ("python", "git", "codex", "pwsh", "cargo", "cmake", "java", "node", "php")},
            "policy": "Detected files/executables are discovery evidence, not successful builds or tests."}
