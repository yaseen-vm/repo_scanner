import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config

CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".less",
    ".vue",
    ".svelte",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".xml",
    ".dockerfile",
    ".tf",
    ".hcl",
}


@dataclass
class FileChange:
    path: str
    content: str
    diff: str
    language: str
    additions: int
    deletions: int


def detect_language(path: str) -> str:
    ext = Path(path).suffix.lower()
    mapping = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".scala": "scala",
        ".sh": "shell",
        ".sql": "sql",
        ".html": "html",
        ".css": "css",
        ".vue": "vue",
        ".svelte": "svelte",
        ".tf": "terraform",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".json": "json",
        ".toml": "toml",
    }
    return mapping.get(ext, "unknown")


def should_ignore(path: str, ignore_patterns: list[str]) -> bool:
    for pattern in ignore_patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _get_files_from_git_diff(base_sha: str, head_sha: str, workspace: str) -> list[FileChange]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, head_sha],
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    if result.returncode != 0:
        return []

    files: list[FileChange] = []
    for file_path in result.stdout.strip().splitlines():
        if not file_path:
            continue
        full_path = Path(workspace) / file_path
        if full_path.exists() and full_path.is_file():
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                files.append(
                    FileChange(
                        path=file_path,
                        content=content,
                        diff="",
                        language=detect_language(file_path),
                        additions=0,
                        deletions=0,
                    )
                )
            except Exception:
                continue
    return files


def get_changed_files_from_event(event_path: str, workspace: str) -> list[FileChange]:
    if not event_path or not Path(event_path).exists():
        return []

    with open(event_path) as f:
        event = json.load(f)

    pull_request = event.get("pull_request", {})
    if pull_request:
        base_sha = pull_request.get("base", {}).get("sha", "")
        head_sha = pull_request.get("head", {}).get("sha", "")
        if base_sha and head_sha:
            return _get_files_from_git_diff(base_sha, head_sha, workspace)
        return []

    head_commit = event.get("head_commit", {})
    if not head_commit:
        return []

    added = head_commit.get("added", [])
    modified = head_commit.get("modified", [])

    files: list[FileChange] = []
    for file_path in added + modified:
        full_path = Path(workspace) / file_path
        if full_path.exists() and full_path.is_file():
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                files.append(
                    FileChange(
                        path=file_path,
                        content=content,
                        diff="",
                        language=detect_language(file_path),
                        additions=0,
                        deletions=0,
                    )
                )
            except Exception:
                continue

    return files


_HIGH_RISK_DIRS = {
    "api", "auth", "payment", "security", "login", "admin",
    "oauth", "webhook", "route", "controller", "handler", "middleware",
}

_LOW_PRIORITY_DIRS = {
    "test", "spec", "mock", "fixture", "vendor", "generated", "migrations",
}


def _get_recently_changed_files(workspace: str) -> set[str]:
    """GAP 6: Get set of recently changed file paths from git log."""
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", "-n", "50"],
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=10,
        )
        if result.returncode == 0:
            paths = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    paths.add(line.replace("\\", "/"))
            return paths
    except Exception:
        pass
    return set()


def _priority_score(relative: str, recent_files: set[str]) -> int:
    """GAP 6: Compute priority score for a file path."""
    score = 0
    rel_lower = relative.lower()

    # +10 for recently changed files
    if relative in recent_files:
        score += 10

    # +5 for high-risk directories
    parts = rel_lower.replace("\\", "/").split("/")
    for part in parts:
        if any(risk in part for risk in _HIGH_RISK_DIRS):
            score += 5
            break

    # +2 for files NOT in low-priority directories
    in_low_priority = any(
        any(lp in part for lp in _LOW_PRIORITY_DIRS) for part in parts
    )
    if not in_low_priority:
        score += 2

    return score


def scan_files(workspace: str, config: Config) -> list[FileChange]:
    workspace_path = Path(workspace)
    candidates: list[tuple[str, str]] = []  # (relative_path, content)

    for file_path in workspace_path.rglob("*"):
        if not file_path.is_file():
            continue

        relative = str(file_path.relative_to(workspace_path)).replace("\\", "/")

        if should_ignore(relative, config.ignore_patterns):
            continue

        if file_path.suffix.lower() not in CODE_EXTENSIONS:
            continue

        if file_path.stat().st_size > config.max_file_size:
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            candidates.append((relative, content))
        except Exception:
            continue

    # GAP 6: Sort candidates by priority score before taking max_files
    recent_files = _get_recently_changed_files(workspace)
    candidates.sort(key=lambda x: _priority_score(x[0], recent_files), reverse=True)

    files: list[FileChange] = []
    for relative, content in candidates[: config.max_files]:
        files.append(
            FileChange(
                path=relative,
                content=content,
                diff="",
                language=detect_language(relative),
                additions=0,
                deletions=0,
            )
        )

    return files
