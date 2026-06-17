from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from github import Github
from github.GithubException import UnknownObjectException

from .analyzer import Issue
from .config import Config

LABEL_DEFS = {
    "critical": {"color": "b60205", "description": "Critical severity issue"},
    "high": {"color": "d93f0b", "description": "High severity issue"},
    "medium": {"color": "fbca04", "description": "Medium severity issue"},
    "low": {"color": "0e8a16", "description": "Low severity issue"},
    "bug": {"color": "d73a4a", "description": "Bug or logic error"},
    "security": {"color": "e11d48", "description": "Security vulnerability"},
    "quality": {"color": "8b5cf6", "description": "Code quality issue"},
    "performance": {"color": "f59e0b", "description": "Performance issue"},
    "repo-scanner": {"color": "1d76db", "description": "Detected by repo scanner"},
    "auto-fix-approved": {
        "color": "0075ca",
        "description": "Approved for auto-fix by repo scanner",
    },
    "fix-in-progress": {
        "color": "e4e669",
        "description": "Auto-fix PR has been created",
    },
    "plan": {"color": "7057ff", "description": "Trigger AI investigation and fix plan"},
    "plan-ready": {"color": "008672", "description": "AI fix plan is ready for review"},
    "approved": {"color": "0075ca", "description": "Fix plan approved — trigger auto-fix"},
    "replan": {"color": "e4e669", "description": "Request a new AI fix plan"},
    "needs-manual-review": {
        "color": "b60205",
        "description": "AI could not generate a plan — manual review needed",
    },
}


def get_github_client(config: Config) -> Github:
    return Github(config.github_token)


def ensure_labels(config: Config) -> None:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)

    existing = {label.name for label in repo.get_labels()}
    missing = [name for name in LABEL_DEFS if name not in existing]

    for name in missing:
        repo.create_label(
            name=name,
            color=LABEL_DEFS[name]["color"],
            description=LABEL_DEFS[name]["description"],
        )


def get_existing_issue_titles(config: Config) -> set[str]:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    return {i.title for i in repo.get_issues(labels=["repo-scanner"], state="open")}


def _normalize_title(title: str) -> str:
    """Normalize title for comparison by removing severity prefix and lowercasing."""
    import re

    # Remove severity prefix like [HIGH], [MEDIUM], etc.
    normalized = re.sub(
        r"^\[(CRITICAL|HIGH|MEDIUM|LOW)\]\s*", "", title, flags=re.IGNORECASE
    )
    return normalized.lower().strip()


def _titles_are_similar(title1: str, title2: str, threshold: float = 0.8) -> bool:
    """Check if two titles are similar enough to be considered duplicates."""
    norm1 = _normalize_title(title1)
    norm2 = _normalize_title(title2)

    # Exact match after normalization
    if norm1 == norm2:
        return True

    # Use SequenceMatcher for fuzzy matching
    ratio = SequenceMatcher(None, norm1, norm2).ratio()
    return ratio >= threshold


def find_duplicate_issues(config: Config, title: str) -> list[dict]:
    """Find existing open issues that are similar to the given title."""
    g = get_github_client(config)
    repo = g.get_repo(config.repo)

    duplicates = []
    for issue in repo.get_issues(labels=["repo-scanner"], state="open"):
        if _titles_are_similar(title, issue.title):
            duplicates.append(
                {
                    "number": issue.number,
                    "title": issue.title,
                    "html_url": issue.html_url,
                }
            )
    return duplicates


def create_issue(
    config: Config,
    issue: Issue,
    commit_sha: str = "",
    existing_titles: set[str] | None = None,
) -> str | None:
    title = f"[{issue.severity.upper()}] {issue.title}"

    # Check for exact duplicates first
    if existing_titles is not None and title in existing_titles:
        return None

    # Check for similar duplicates (fuzzy matching)
    duplicates = find_duplicate_issues(config, title)
    if duplicates:
        # Close the duplicate issue instead of creating a new one
        return None

    g = get_github_client(config)
    repo = g.get_repo(config.repo)

    labels = [issue.severity, issue.category, "repo-scanner"]

    body_parts = [
        f"**Severity:** {issue.severity}",
        f"**Category:** {issue.category}",
        f"**File:** `{issue.file}:{issue.line}`",
        "",
        "## Description",
        issue.description,
        "",
        "## Suggested Fix",
        issue.suggestion,
    ]
    if commit_sha:
        body_parts.append(f"\n---\nDetected in commit `{commit_sha[:8]}`")

    created = repo.create_issue(title=title, body="\n".join(body_parts), labels=labels)
    return created.html_url


def post_pr_comment(config: Config, pr_number: int, body: str) -> str:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    pr = repo.get_pull(pr_number)
    comment = pr.create_issue_comment(body)
    return comment.html_url


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _parse_raw_issue(issue) -> dict:
    label_names = [l.name for l in issue.labels]
    severity = "medium"
    file_path = ""
    line = 0

    for lbl in label_names:
        if lbl in _SEVERITY_RANK:
            severity = lbl
            break

    import re as _re

    body_lines = (issue.body or "").split("\n")
    for line_text in body_lines:
        if line_text.startswith("**File:**"):
            ref = line_text.split("`")[1] if "`" in line_text else ""
            if ":" in ref:
                file_path, _, line_str = ref.rpartition(":")
                try:
                    line = int(line_str)
                except ValueError:
                    line = 0
            else:
                file_path = ref
            break

    # Fallback: parse "## Affected Files" / "## Affected File" bullet list
    if not file_path:
        in_affected = False
        for line_text in body_lines:
            if _re.match(r"^##\s+Affected Files?", line_text, _re.IGNORECASE):
                in_affected = True
                continue
            if in_affected:
                if line_text.startswith("#"):
                    break
                m = _re.search(r"`([^`]+)`", line_text)
                if m:
                    ref = m.group(1)
                    if ":" in ref:
                        file_path, _, line_str = ref.rpartition(":")
                        try:
                            line = int(line_str)
                        except ValueError:
                            line = 0
                    else:
                        file_path = ref
                    break

    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "labels": label_names,
        "severity": severity,
        "file": file_path,
        "line": line,
        "html_url": issue.html_url,
        "created_at": issue.created_at,
    }


def get_scanner_issues(
    config: Config,
    severity_filter: str | None = None,
    min_age_days: int = 0,
    issue_number: int | None = None,
) -> list[dict]:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)

    if issue_number is not None:
        raw = repo.get_issue(issue_number)
        parsed = _parse_raw_issue(raw)
        return [parsed]

    min_rank = _SEVERITY_RANK.get(severity_filter, 0) if severity_filter else 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=min_age_days)
        if min_age_days > 0
        else None
    )

    parsed = []
    for issue in repo.get_issues(labels=["repo-scanner"], state="open"):
        entry = _parse_raw_issue(issue)

        if _SEVERITY_RANK.get(entry["severity"], 0) < min_rank:
            continue

        if cutoff and entry["created_at"] > cutoff:
            continue

        parsed.append(entry)

    parsed.sort(key=lambda x: _SEVERITY_RANK.get(x["severity"], 0), reverse=True)
    return parsed


def create_pull_request(
    config: Config,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str = "main",
) -> str:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    pr = repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
    return pr.html_url


def close_duplicate_issue(config: Config, issue_number: int, duplicate_of: int) -> bool:
    """Close an issue as a duplicate of another issue."""
    try:
        g = get_github_client(config)
        repo = g.get_repo(config.repo)
        issue = repo.get_issue(issue_number)

        # Add a comment explaining why it's being closed
        comment_body = (
            f"🔄 **Closing as duplicate**\n\n"
            f"This issue is a duplicate of #{duplicate_of} and has been automatically closed.\n\n"
            f"The original issue will be tracked and fixed."
        )
        issue.create_comment(comment_body)

        # Close the issue
        issue.edit(state="closed")
        return True
    except Exception as e:
        print(f"Failed to close duplicate issue #{issue_number}: {e}")
        return False


def close_resolved_issue(config: Config, issue_number: int, reason: str) -> bool:
    """Close an issue that has already been resolved (e.g., file deleted)."""
    try:
        g = get_github_client(config)
        repo = g.get_repo(config.repo)
        issue = repo.get_issue(issue_number)

        # Add a comment explaining why it's being closed
        comment_body = (
            f"✅ **Issue already resolved**\n\n"
            f"{reason}\n\n"
            f"This issue has been automatically closed as the problem no longer exists."
        )
        issue.create_comment(comment_body)

        # Close the issue
        issue.edit(state="closed")
        return True
    except Exception as e:
        print(f"Failed to close resolved issue #{issue_number}: {e}")
        return False


def post_issue_comment(config: Config, issue_number: int, body: str) -> str:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    issue = repo.get_issue(issue_number)
    comment = issue.create_comment(body)
    return comment.html_url


def get_latest_plan_comment(config: Config, issue_number: int) -> dict | None:
    """Return the most recent plan comment on an issue, or None."""
    from .planner import PLAN_MARKER, parse_plan_comment

    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    issue = repo.get_issue(issue_number)

    last = None
    for comment in issue.get_comments():
        if PLAN_MARKER in (comment.body or ""):
            parsed = parse_plan_comment(comment.body)
            if parsed:
                last = {"id": comment.id, "body": comment.body, **parsed}
    return last


def delete_plan_comments(config: Config, issue_number: int) -> None:
    """Delete all plan comments from an issue."""
    from .planner import PLAN_MARKER

    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    issue = repo.get_issue(issue_number)

    for comment in issue.get_comments():
        if PLAN_MARKER in (comment.body or ""):
            comment.delete()


def swap_label(config: Config, issue_number: int, remove: str, add: str) -> None:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    issue = repo.get_issue(issue_number)
    try:
        issue.remove_from_labels(remove)
    except Exception:
        pass
    issue.add_to_labels(add)


def get_pr_number_from_event(event_path: str) -> int | None:
    import json
    from pathlib import Path

    if not event_path or not Path(event_path).exists():
        return None

    with open(event_path) as f:
        event = json.load(f)

    pr = event.get("pull_request", {})
    return pr.get("number")
