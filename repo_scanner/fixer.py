import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from .config import Config
from .github_client import create_pull_request, get_scanner_issues

_TEXT_BYTES = bytearray(
    {7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F}
)


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    non_text = sum(1 for b in chunk if b not in _TEXT_BYTES)
    return non_text / max(len(chunk), 1) > 0.30

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
) -> str:
    issue_number = issue["number"]
    slug = _slugify(issue["title"])
    branch_name = f"fix/{issue_number}-{slug}"
    base_branch = _get_default_branch(config, workspace)

    file_path = Path(workspace) / issue["file"]

    result = _run_git(["checkout", base_branch], cwd=workspace)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout {base_branch} failed: {result.stderr}")

    result = _run_git(["checkout", "-b", branch_name], cwd=workspace)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout -b {branch_name} failed: {result.stderr}")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(fix_content, encoding="utf-8")

        result = _run_git(["add", issue["file"]], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git add failed: {result.stderr}")

        commit_msg = f"fix: {issue['title']}\n\nCloses #{issue_number}"
        result = _run_git(["commit", "-m", commit_msg], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr}")

        result = _run_git(["push", "origin", branch_name], cwd=workspace)
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


def run_fixes(
    config: Config,
    max_fixes: int,
    severity_filter: str | None,
    workspace: str,
    issue_number: int | None = None,
    min_age_days: int = 0,
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

            pr_url = apply_fix_and_create_pr(config, issue, fix_content, workspace)
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

    return results
