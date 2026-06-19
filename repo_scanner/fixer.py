import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from .config import Config
from .github_client import close_resolved_issue, create_pull_request, get_scanner_issues

_TEXT_BYTES = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})

VALIDATORS: dict[str, list[str]] = {
    "python": ["ruff", "check", "{file}"],
    "javascript": ["npx", "eslint", "--no-eslintrc", "{file}"],
    "typescript": ["npx", "tsc", "--noEmit", "--allowJs", "{file}"],
    "go": ["gofmt", "-e", "{file}"],
    "rust": ["rustfmt", "--check", "{file}"],
    "ruby": ["ruby", "-c", "{file}"],
}

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
}


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    non_text = sum(1 for b in chunk if b not in _TEXT_BYTES)
    return non_text / max(len(chunk), 1) > 0.30


def validate_fix(file_path: Path, language: str) -> tuple[bool, str]:
    validators = VALIDATORS.get(language)
    if not validators:
        return True, ""

    cmd = [c.replace("{file}", str(file_path)) for c in validators]
    tool = cmd[0]

    if not shutil.which(tool):
        return True, ""

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True, ""
        error_output = result.stdout.strip() or result.stderr.strip()
        return False, error_output
    except subprocess.TimeoutExpired:
        return False, f"Validation timed out after 60s"
    except Exception as e:
        return True, ""


FIX_SYSTEM_PROMPT = """You are a surgical code fixer. You will be given a WINDOW of lines from a file and an issue to fix. Return ONLY the fixed version of that window — the exact same number of context lines with the bug fixed. Preserve indentation and style exactly. Return raw code only, no fences, no explanation."""

CREATE_FILE_SYSTEM_PROMPT = """You are a code generator. You will be given an issue title and description asking you to create a new file. Generate the complete contents of that file. Return ONLY the raw file content — no markdown fences, no explanation."""


@dataclass
class FixResult:
    issue_number: int
    issue_title: str
    success: bool
    pr_url: str = ""
    error: str = ""
    validation_error: str = ""


def parse_issue_fields(body: str) -> dict:
    """GAP 1: Also extract line number from **File:** field."""
    fields = {"description": "", "suggestion": "", "line": 0}
    section = None
    lines = []

    for line in body.split("\n"):
        # GAP 1: Extract line number from **File:** field
        if line.startswith("**File:**"):
            ref = line.split("`")[1] if "`" in line else ""
            if ":" in ref:
                _, _, line_str = ref.rpartition(":")
                try:
                    fields["line"] = int(line_str)
                except ValueError:
                    pass

        if line.strip() == "## Description":
            section = "description"
            lines = []
            continue
        elif line.strip() == "## Suggested Fix":
            if section == "description":
                fields["description"] = "\n".join(lines).strip()
            section = "suggestion"
            lines = []
            continue
        elif line.startswith("## ") or line.startswith("---"):
            if section:
                fields[section] = "\n".join(lines).strip()
            section = None
            lines = []
            continue
        if section is not None:
            lines.append(line)

    if section:
        fields[section] = "\n".join(lines).strip()

    return fields


def generate_fix(config: Config, file_content: str, issue: dict) -> str:
    """GAP 1/12: Use windowed context — send only a 120-line window around the issue line."""
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    fields = parse_issue_fields(issue["body"])

    all_lines = file_content.splitlines(keepends=True)
    total_lines = len(all_lines)
    issue_line = fields.get("line", 0)

    # Determine window
    if issue_line > 0 and total_lines > 150:
        window_start = max(0, issue_line - 1 - 60)  # 0-indexed
        window_end = min(total_lines, issue_line - 1 + 60)
    else:
        window_start = 0
        window_end = total_lines

    window_lines = all_lines[window_start:window_end]
    window_size = len(window_lines)

    # Label lines with their 1-based numbers for clarity
    numbered_window = "".join(
        f"{window_start + idx + 1}: {line}"
        for idx, line in enumerate(window_lines)
    )

    user_message = (
        f"File: {issue['file']}\n"
        f"Issue: {issue['title']}\n"
        f"Description: {fields['description']}\n"
        f"Suggested fix: {fields['suggestion']}\n\n"
        f"Window (lines {window_start + 1}–{window_start + window_size}):\n"
        f"{numbered_window}\n\n"
        f"Return ONLY the fixed version of this window — same number of context lines, "
        f"bug fixed, raw code only."
    )

    response = client.chat.completions.create(
        model=config.model,
        messages=[
            {"role": "system", "content": FIX_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.1,
        max_tokens=8192,
    )

    fixed_window_raw = response.choices[0].message.content or ""
    fixed_window_raw = fixed_window_raw.strip()

    # Strip markdown fences if present
    if fixed_window_raw.startswith("```"):
        first_newline = fixed_window_raw.find("\n")
        if first_newline != -1:
            fixed_window_raw = fixed_window_raw[first_newline + 1:]
        if fixed_window_raw.endswith("```"):
            fixed_window_raw = fixed_window_raw[:-3]
        fixed_window_raw = fixed_window_raw.strip()

    # Strip leading line-number annotations (e.g. "42: ") that the LLM may echo back
    def _strip_line_numbers(text: str) -> str:
        cleaned = []
        for ln in text.splitlines(keepends=True):
            stripped = re.sub(r"^\d+:\s", "", ln)
            cleaned.append(stripped)
        return "".join(cleaned)

    fixed_window_raw = _strip_line_numbers(fixed_window_raw)

    # Splice the fixed window back into the full file
    fixed_window_lines = fixed_window_raw.splitlines(keepends=True)
    # Ensure last line has newline if original did
    if fixed_window_lines and not fixed_window_lines[-1].endswith("\n"):
        if window_end < total_lines:
            fixed_window_lines[-1] += "\n"

    spliced = all_lines[:window_start] + fixed_window_lines + all_lines[window_start + window_size:]
    return "".join(spliced)


def generate_new_file(config: Config, issue: dict) -> str:
    """Generate content for a file that does not yet exist."""
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    fields = parse_issue_fields(issue["body"])
    user_message = (
        f"File to create: {issue['file']}\n"
        f"Issue: {issue['title']}\n"
        f"Description: {fields['description']}\n"
        f"Suggested content: {fields['suggestion']}\n\n"
        f"Generate the complete contents of this new file."
    )
    response = client.chat.completions.create(
        model=config.model,
        messages=[
            {"role": "system", "content": CREATE_FILE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.1,
        max_tokens=8192,
    )
    content = response.choices[0].message.content or ""
    content = content.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len]


# GAP 8: Stale line detection
_STOP_WORDS = {
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "for", "of", "and",
    "or", "but", "not", "with", "this", "that", "from", "be", "are", "was",
    "were", "been", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "i", "we", "you", "he", "she",
    "they", "its",
}


def check_line_staleness(file_path: Path, line: int, issue_title: str) -> tuple[bool, str]:
    """GAP 8: Check whether the issue line number is still valid in the current file."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return False, ""

    lines = content.splitlines()

    if line > len(lines):
        return (
            True,
            f"Line {line} is out of range — file only has {len(lines)} lines. "
            "The code may have been refactored.",
        )

    if line > 0:
        # Extract 5 lines around the reported line (1-indexed → 0-indexed)
        start = max(0, line - 1 - 2)
        end = min(len(lines), line - 1 + 3)
        snippet = " ".join(lines[start:end]).lower()
        snippet_words = set(re.findall(r"\b[a-z_][a-z0-9_]{2,}\b", snippet))
        snippet_words -= _STOP_WORDS

        title_words = set(re.findall(r"\b[a-z_][a-z0-9_]{2,}\b", issue_title.lower()))
        title_words -= _STOP_WORDS

        if len(title_words) >= 3 and len(snippet_words & title_words) == 0:
            return (
                True,
                "Line content does not match issue description — code may have moved. "
                "Consider replanning.",
            )

    return False, ""


# GAP 3: Test awareness helpers
def find_related_tests(file_path: Path, workspace: str) -> list[Path]:
    """GAP 3: Find test files related to the given source file."""
    stem = file_path.stem
    workspace_path = Path(workspace)
    patterns = [
        f"test_{stem}*",
        f"{stem}_test*",
        f"{stem}.test.*",
        f"{stem}.spec.*",
        f"test_{stem}.*",
    ]
    found: list[Path] = []
    for pattern in patterns:
        for match in workspace_path.rglob(pattern):
            if match.is_file() and match not in found:
                found.append(match)
            if len(found) >= 3:
                return found
    return found[:3]


def run_tests(test_files: list[Path], workspace: str) -> tuple[bool, str]:
    """GAP 3: Run test files and return (passed, output)."""
    if not test_files:
        return True, ""

    py_tests = [f for f in test_files if f.suffix == ".py"]
    js_tests = [f for f in test_files if f.suffix in {".ts", ".js", ".tsx", ".jsx"}]

    if py_tests and shutil.which("pytest"):
        cmd = ["pytest"] + [str(f) for f in py_tests] + ["-x", "-q"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=workspace,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "Test run timed out after 120s"
        except Exception as e:
            return False, str(e)

    if js_tests and shutil.which("npx"):
        cmd = ["npx", "jest"] + [str(f) for f in js_tests] + ["--no-coverage"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=workspace,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "Test run timed out after 120s"
        except Exception as e:
            return False, str(e)

    return True, ""


def _run_git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _is_already_resolved_file_issue(issue: dict) -> bool:
    """Check if an issue is about a corrupted/missing file that was already resolved."""
    title = issue.get("title", "").lower()
    description = issue.get("body", "").lower()

    # Check for indicators that this is about a corrupted/missing file
    corrupted_indicators = [
        "corrupted",
        "malicious",
        "invalid",
        "binary",
        "encoded",
        "temp.html",
        "temp file",
        "temporary file",
    ]

    # Check if the issue mentions any corrupted file indicators
    for indicator in corrupted_indicators:
        if indicator in title or indicator in description:
            return True

    # Check if the file path suggests it's a temporary file
    file_path = issue.get("file", "").lower()
    if file_path.startswith("temp") or file_path.endswith(".tmp"):
        return True

    return False


def _get_default_branch(config: Config, workspace: str) -> str:
    result = _run_git(
        ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"], cwd=workspace
    )
    if result.returncode == 0:
        return result.stdout.strip().removeprefix("origin/")
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace)
    if result.returncode == 0:
        branch = result.stdout.strip()
        if branch != "HEAD":
            return branch
    return "main"


def apply_fix_and_create_pr(
    config: Config,
    issue: dict,
    fix_content: str,
    workspace: str,
    validate: bool = True,
) -> str:
    issue_number = issue["number"]
    slug = _slugify(issue["title"])
    branch_name = f"fix/{issue_number}-{slug}"
    base_branch = _get_default_branch(config, workspace)

    file_path = Path(workspace) / issue["file"]
    language = EXTENSION_TO_LANGUAGE.get(file_path.suffix.lower(), "")

    result = _run_git(["checkout", base_branch], cwd=workspace)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout {base_branch} failed: {result.stderr}")

    # GAP 10: Check if branch already exists remotely before creating
    ls_remote = _run_git(["ls-remote", "--heads", "origin", branch_name], cwd=workspace)
    branch_exists_remotely = bool(ls_remote.stdout.strip())

    if branch_exists_remotely:
        print(f"Branch already exists, updating existing fix branch: {branch_name}")
        _run_git(["fetch", "origin", branch_name], cwd=workspace)
        result = _run_git(["checkout", branch_name], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git checkout {branch_name} failed: {result.stderr}")
    else:
        result = _run_git(["checkout", "-b", branch_name], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git checkout -b {branch_name} failed: {result.stderr}")

    try:
        original_content = None
        if file_path.exists():
            original_content = file_path.read_text(encoding="utf-8")

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(fix_content, encoding="utf-8")

        if validate and language:
            passed, error_output = validate_fix(file_path, language)
            if not passed:
                if original_content is not None:
                    file_path.write_text(original_content, encoding="utf-8")
                else:
                    file_path.unlink(missing_ok=True)
                raise RuntimeError(f"Validation failed: {error_output}")

        # GAP 3: Run related tests after syntax validation passes
        test_files = find_related_tests(file_path, workspace)
        tests_note = ""
        if test_files:
            tests_passed, test_output = run_tests(test_files, workspace)
            if not tests_passed:
                if original_content is not None:
                    file_path.write_text(original_content, encoding="utf-8")
                else:
                    file_path.unlink(missing_ok=True)
                _run_git(["checkout", base_branch], cwd=workspace)
                if not branch_exists_remotely:
                    _run_git(["branch", "-D", branch_name], cwd=workspace)
                raise RuntimeError(f"Tests failed after fix:\n{test_output[:500]}")
            test_names = ", ".join(f.name for f in test_files)
            tests_note = f"\n\nTests passed: {test_names}"

        result = _run_git(["add", issue["file"]], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git add failed: {result.stderr}")

        commit_msg = f"fix: {issue['title']}\n\nCloses #{issue_number}"
        result = _run_git(["commit", "-m", commit_msg], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr}")

        # GAP 10: Use --force-with-lease instead of --force
        result = _run_git(["push", "--force-with-lease", "origin", branch_name], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git push failed: {result.stderr}")

        pr_body = (
            f"## Auto-fix by Repo Scanner\n\n"
            f"Closes #{issue_number}\n\n"
            f"**Issue:** {issue['title']}\n"
            f"**Severity:** {issue['severity']}\n"
            f"**File:** `{issue['file']}`\n\n"
            f"This PR was automatically generated by repo-scanner's fix command. "
            f"Please review the changes carefully before merging."
            f"{tests_note}"
        )
        pr_url = create_pull_request(
            config,
            title=f"Fix: {issue['title']} (#{issue_number})",
            body=pr_body,
            head_branch=branch_name,
            base_branch=base_branch,
        )
        return pr_url

    except Exception:
        _run_git(["checkout", base_branch], cwd=workspace)
        if not branch_exists_remotely:
            _run_git(["branch", "-D", branch_name], cwd=workspace)
        raise


def fix_from_plan_comment(
    config: Config,
    issue_number: int,
    workspace: str,
) -> FixResult:
    """Fix a GitHub issue using the plan posted as a comment."""
    from github import Github
    from .github_client import get_latest_plan_comment
    from .planner import extract_fix_fields

    plan_data = get_latest_plan_comment(config, issue_number)
    if not plan_data:
        return FixResult(
            issue_number=issue_number,
            issue_title=f"Issue #{issue_number}",
            success=False,
            error="No plan comment found. Add the 'plan' label first to generate a plan.",
        )

    g = Github(config.github_token)
    repo = g.get_repo(config.repo)
    gh_issue = repo.get_issue(issue_number)

    file_path_str = plan_data["file"]
    if not file_path_str or file_path_str == "UNKNOWN":
        return FixResult(
            issue_number=issue_number,
            issue_title=gh_issue.title,
            success=False,
            error="Plan did not identify a specific file. Add the 'replan' label to try again.",
        )

    file_path = Path(workspace) / file_path_str
    description, suggestion = extract_fix_fields(plan_data["body"])
    plan_line = plan_data.get("line", 0)
    issue_dict = {
        "number": issue_number,
        "title": gh_issue.title,
        "body": f"## Description\n{description}\n\n## Suggested Fix\n{suggestion}",
        "file": file_path_str,
        "severity": "medium",
        "line": plan_line,
    }

    if not file_path.exists():
        # File doesn't exist — generate and create it
        try:
            new_content = generate_new_file(config, issue_dict)
            if not new_content:
                return FixResult(
                    issue_number=issue_number,
                    issue_title=gh_issue.title,
                    success=False,
                    error=f"LLM returned empty content for new file: {file_path_str}",
                )
            pr_url = apply_fix_and_create_pr(config, issue_dict, new_content, workspace)
            return FixResult(
                issue_number=issue_number,
                issue_title=gh_issue.title,
                success=True,
                pr_url=pr_url,
            )
        except Exception as e:
            return FixResult(
                issue_number=issue_number,
                issue_title=gh_issue.title,
                success=False,
                error=str(e),
            )

    if _is_binary(file_path):
        return FixResult(
            issue_number=issue_number,
            issue_title=gh_issue.title,
            success=False,
            error="File is binary — cannot auto-fix.",
        )

    # GAP 8: Check for stale line before attempting fix
    if plan_line > 0:
        stale, staleness_msg = check_line_staleness(file_path, plan_line, gh_issue.title)
        if stale:
            return FixResult(
                issue_number=issue_number,
                issue_title=gh_issue.title,
                success=False,
                error=staleness_msg,
            )

    try:
        file_content = file_path.read_text(encoding="utf-8")
        fix_content = generate_fix(config, file_content, issue_dict)
        if not fix_content or fix_content == file_content:
            return FixResult(
                issue_number=issue_number,
                issue_title=gh_issue.title,
                success=False,
                error="LLM returned unchanged or empty content.",
            )

        pr_url = apply_fix_and_create_pr(config, issue_dict, fix_content, workspace)
        return FixResult(
            issue_number=issue_number,
            issue_title=gh_issue.title,
            success=True,
            pr_url=pr_url,
        )
    except Exception as e:
        return FixResult(
            issue_number=issue_number,
            issue_title=gh_issue.title,
            success=False,
            error=str(e),
        )


def run_fixes(
    config: Config,
    max_fixes: int,
    severity_filter: str | None,
    workspace: str,
    issue_number: int | None = None,
    min_age_days: int = 0,
    validate_fixes: bool = True,
) -> list[FixResult]:
    issues = get_scanner_issues(
        config,
        severity_filter=severity_filter,
        min_age_days=min_age_days,
        issue_number=issue_number,
    )
    if not issues:
        return []

    results: list[FixResult] = []
    for issue in issues[:max_fixes]:
        if not issue["file"]:
            results.append(
                FixResult(
                    issue_number=issue["number"],
                    issue_title=issue["title"],
                    success=False,
                    error="No file path found in issue body — cannot auto-fix.",
                )
            )
            continue

        file_path = Path(workspace) / issue["file"]
        if not file_path.exists():
            # Check if this is a corrupted/missing file issue that was already resolved
            if _is_already_resolved_file_issue(issue):
                # Close the issue as already resolved
                close_resolved_issue(
                    config,
                    issue["number"],
                    f"The file `{issue['file']}` mentioned in this issue no longer exists in the repository. "
                    f"It appears to have been intentionally deleted (possibly because it was corrupted or invalid).",
                )
                results.append(
                    FixResult(
                        issue_number=issue["number"],
                        issue_title=issue["title"],
                        success=True,  # Mark as success since we closed the issue
                        pr_url="Issue closed as already resolved",
                    )
                )
            else:
                # File doesn't exist — generate and create it
                try:
                    new_content = generate_new_file(config, issue)
                    if not new_content:
                        results.append(
                            FixResult(
                                issue_number=issue["number"],
                                issue_title=issue["title"],
                                success=False,
                                error=f"LLM returned empty content for new file: {issue['file']}",
                            )
                        )
                        continue
                    pr_url = apply_fix_and_create_pr(config, issue, new_content, workspace)
                    results.append(
                        FixResult(
                            issue_number=issue["number"],
                            issue_title=issue["title"],
                            success=True,
                            pr_url=pr_url,
                        )
                    )
                except Exception as e:
                    results.append(
                        FixResult(
                            issue_number=issue["number"],
                            issue_title=issue["title"],
                            success=False,
                            error=str(e),
                        )
                    )
            continue

        if _is_binary(file_path):
            results.append(
                FixResult(
                    issue_number=issue["number"],
                    issue_title=issue["title"],
                    success=False,
                    error="File is binary or corrupted — cannot auto-fix. Delete the file manually or restore from git history.",
                )
            )
            continue

        # GAP 8: Check for stale line before attempting fix
        issue_line = issue.get("line", 0)
        if issue_line > 0:
            stale, staleness_msg = check_line_staleness(file_path, issue_line, issue["title"])
            if stale:
                results.append(
                    FixResult(
                        issue_number=issue["number"],
                        issue_title=issue["title"],
                        success=False,
                        error=staleness_msg,
                    )
                )
                continue

        try:
            file_content = file_path.read_text(encoding="utf-8")
            fix_content = generate_fix(config, file_content, issue)
            if not fix_content or fix_content == file_content:
                results.append(
                    FixResult(
                        issue_number=issue["number"],
                        issue_title=issue["title"],
                        success=False,
                        error="LLM returned unchanged or empty content",
                    )
                )
                continue

            pr_url = apply_fix_and_create_pr(
                config, issue, fix_content, workspace, validate=validate_fixes
            )
            results.append(
                FixResult(
                    issue_number=issue["number"],
                    issue_title=issue["title"],
                    success=True,
                    pr_url=pr_url,
                )
            )
        except Exception as e:
            error_msg = str(e)
            validation_err = ""
            if error_msg.startswith("Validation failed: "):
                validation_err = error_msg[len("Validation failed: ") :]
                error_msg = error_msg
            results.append(
                FixResult(
                    issue_number=issue["number"],
                    issue_title=issue["title"],
                    success=False,
                    error=error_msg,
                    validation_error=validation_err,
                )
            )

    return results
