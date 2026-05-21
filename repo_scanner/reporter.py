import json
from datetime import datetime
from pathlib import Path

from .analyzer import Issue


def generate_markdown_report(
    issues: list[Issue], repo: str, commit_sha: str = ""
) -> str:
    lines = [
        "# Repo Scanner Report",
        "",
        f"**Repository:** {repo}",
        f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ]
    if commit_sha:
        lines.append(f"**Commit:** `{commit_sha[:8]}`")
    lines.append(f"**Total Issues:** {len(issues)}")
    lines.append("")

    if not issues:
        lines.append("No issues found! Code looks clean.")
        return "\n".join(lines)

    severity_counts = {}
    category_counts = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
        category_counts[issue.category] = category_counts.get(issue.category, 0) + 1

    lines.append("## Summary")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|----------|-------|")
    for sev in ["critical", "high", "medium", "low"]:
        if sev in severity_counts:
            lines.append(f"| {sev} | {severity_counts[sev]} |")
    lines.append("")

    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    for cat in ["security", "bug", "performance", "quality"]:
        if cat in category_counts:
            lines.append(f"| {cat} | {category_counts[cat]} |")
    lines.append("")

    lines.append("## Issues")
    lines.append("")

    for i, issue in enumerate(issues, 1):
        emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
            issue.severity, "⚪"
        )
        lines.append(f"### {emoji} {i}. {issue.title}")
        lines.append("")
        lines.append(f"- **Severity:** {issue.severity}")
        lines.append(f"- **Category:** {issue.category}")
        lines.append(f"- **File:** `{issue.file}:{issue.line}`")
        lines.append("")
        lines.append(f"**Description:** {issue.description}")
        lines.append("")
        lines.append(f"**Suggestion:** {issue.suggestion}")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


SEVERITY_TO_SARIF_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}


def _slugify(text: str) -> str:
    return text.lower().replace(" ", "-").replace("_", "-")


def generate_sarif_report(issues: list[Issue], repo: str, commit_sha: str = "") -> str:
    results = []
    for issue in issues:
        rule_id = f"{issue.category}/{_slugify(issue.title)}"
        level = SEVERITY_TO_SARIF_LEVEL.get(issue.severity, "warning")
        results.append(
            {
                "ruleId": rule_id,
                "level": level,
                "message": {"text": issue.description},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": issue.file,
                                "index": 0,
                            },
                            "region": {
                                "startLine": max(issue.line, 1),
                            },
                        }
                    }
                ],
            }
        )

    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "repo-scanner",
                        "informationUri": "https://github.com/yaseen-vm/repo_scanner",
                        "rules": [],
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2)


def generate_json_report(issues: list[Issue], repo: str, commit_sha: str = "") -> str:
    report = {
        "repo": repo,
        "commit": commit_sha,
        "timestamp": datetime.utcnow().isoformat(),
        "total_issues": len(issues),
        "issues": [
            {
                "file": i.file,
                "line": i.line,
                "severity": i.severity,
                "category": i.category,
                "title": i.title,
                "description": i.description,
                "suggestion": i.suggestion,
            }
            for i in issues
        ],
    }
    return json.dumps(report, indent=2)


def save_report(content: str, output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(content)


def build_pr_summary(issues: list[Issue]) -> str:
    if not issues:
        return "## Repo Scanner\n\nNo issues found! Code looks clean. ✅"

    severity_counts = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1

    lines = [
        "## Repo Scanner Report",
        "",
        f"Found **{len(issues)}** issue(s):",
        "",
    ]

    for sev in ["critical", "high", "medium", "low"]:
        if sev in severity_counts:
            emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
                sev, "⚪"
            )
            lines.append(f"- {emoji} **{sev.capitalize()}:** {severity_counts[sev]}")

    lines.append("")
    lines.append("<details>")
    lines.append("<summary>View all issues</summary>")
    lines.append("")

    for i, issue in enumerate(issues, 1):
        lines.append(
            f"**{i}. [{issue.severity.upper()}]** `{issue.file}:{issue.line}` — {issue.title}"
        )
        lines.append(f"   {issue.description}")
        lines.append("")

    lines.append("</details>")
    return "\n".join(lines)
