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


def create_issue(config: Config, issue: Issue, commit_sha: str = "") -> str:
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

    title = f"[{issue.severity.upper()}] {issue.title}"
    created = repo.create_issue(title=title, body="\n".join(body_parts), labels=labels)
    return created.html_url


def post_pr_comment(config: Config, pr_number: int, body: str) -> str:
    g = get_github_client(config)
    repo = g.get_repo(config.repo)
    pr = repo.get_pull(pr_number)
    comment = pr.create_issue_comment(body)
    return comment.html_url


def get_pr_number_from_event(event_path: str) -> int | None:
    import json
    from pathlib import Path

    if not event_path or not Path(event_path).exists():
        return None

    with open(event_path) as f:
        event = json.load(f)

    pr = event.get("pull_request", {})
    return pr.get("number")
