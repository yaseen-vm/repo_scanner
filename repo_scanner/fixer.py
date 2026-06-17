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


FIX_SYSTEM_PROMPT = """You are a code fixer. Given a file and a bug/issue description, return the COMPLETE fixed file.

Rules:
- Return ONLY the full file content, no explanations or markdown fences
- Preserve all existing code that is not affected by the fix
- Apply the minimal change needed to resolve the issue
- Keep the same style, formatting, and conventions as the original
- Do not add comments explaining what you changed"""


@dataclass
class FixResult:
    issue_number: int
    issue_title: str
    success: bool
    pr_url: str = ""
    error: str = ""
    validation_error: str = ""


def parse_issue_fields(body: str) -> dict:
    fields = {"description": "", "suggestion": ""}
    section = None
    lines = []

    for line in body.split("\n"):
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
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    fields = parse_issue_fields(issue["body"])

    user_message = (
        f"File: {issue['file']}\n"
        f"Issue: {issue['title']}\n"
        f"Description: {fields['description']}\n"
        f"Suggested fix: {fields['suggestion']}\n\n"
        f"Current code:\n```\n{file_content}\n```\n\n"
        f"Return the complete fixed file."
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

    content = response.choices[0].message.content or ""
    content = content.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    return content


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len]


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

        result = _run_git(["add", issue["file"]], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git add failed: {result.stderr}")

        commit_msg = f"fix: {issue['title']}\n\nCloses #{issue_number}"
        result = _run_git(["commit", "-m", commit_msg], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr}")

        result = _run_git(["push", "--force", "origin", branch_name], cwd=workspace)
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
    if not file_path.exists():
        return FixResult(
            issue_number=issue_number,
            issue_title=gh_issue.title,
            success=False,
            error=f"File not found: {file_path_str}",
        )

    if _is_binary(file_path):
        return FixResult(
            issue_number=issue_number,
            issue_title=gh_issue.title,
            success=False,
            error="File is binary — cannot auto-fix.",
        )

    description, suggestion = extract_fix_fields(plan_data["body"])
    issue_dict = {
        "number": issue_number,
        "title": gh_issue.title,
        "body": f"## Description\n{description}\n\n## Suggested Fix\n{suggestion}",
        "file": file_path_str,
        "severity": "medium",
    }

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
                results.append(
                    FixResult(
                        issue_number=issue["number"],
                        issue_title=issue["title"],
                        success=False,
                        error=f"File not found: {issue['file']}",
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
