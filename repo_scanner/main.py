import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .analyzer import analyze_files, filter_by_threshold
from .config import Config
from .fixer import FixResult, fix_from_plan_comment, run_fixes
from .github_client import (
    close_duplicate_issue,
    close_resolved_issue,
    create_issue,
    delete_plan_comments,
    ensure_labels,
    find_duplicate_issues,
    get_existing_issue_titles,
    get_latest_plan_comment,
    get_pr_number_from_event,
    post_issue_comment,
    post_pr_comment,
    swap_label,
)
from .reporter import (
    build_pr_summary,
    generate_json_report,
    generate_markdown_report,
    generate_sarif_report,
    save_report,
)
from .notifications import notify_scan_completed, notify_fix_completed
from .scanner import FileChange, get_changed_files_from_event, scan_files

console = Console()


def print_results(issues, created_urls=None):
    if not issues:
        console.print("[green]No issues found![/green]")
        return

    table = Table(title="Repo Scanner Results")
    table.add_column("Severity", style="bold")
    table.add_column("Category")
    table.add_column("File")
    table.add_column("Line")
    table.add_column("Title")

    severity_styles = {
        "critical": "red bold",
        "high": "red",
        "medium": "yellow",
        "low": "blue",
    }

    for issue in issues:
        style = severity_styles.get(issue.severity, "white")
        table.add_row(
            f"[{style}]{issue.severity}[/{style}]",
            issue.category,
            issue.file,
            str(issue.line),
            issue.title,
        )

    console.print(table)

    if created_urls:
        console.print("\n[bold]Created GitHub Issues:[/bold]")
        for url in created_urls:
            console.print(f"  {url}")


def _require_api_key(cfg: Config):
    if not cfg.api_key:
        console.print(
            "[red]Error: No API key set. Use LLM_API_KEY or MIMO_API_KEY env var.[/red]"
        )
        sys.exit(1)


def _require_repo(cfg: Config):
    if not cfg.repo:
        console.print(
            "[red]Error: No repo set. Use GITHUB_REPOSITORY env var or --repo in config.[/red]"
        )
        sys.exit(1)


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--config", "-c", default=None, help="Path to config YAML file")
def cli(ctx, config):
    """AI-powered repository scanner for GitHub workflows."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    if ctx.invoked_subcommand is None:
        ctx.invoke(scan, config=config)


@cli.command()
@click.option("--config", "-c", default=None, help="Path to config YAML file")
@click.option(
    "--output", "-o", default="repo-scanner-report.md", help="Output report path"
)
@click.option("--output-json", default=None, help="Output JSON report path")
@click.option(
    "--output-sarif",
    default=None,
    help="Output SARIF report path (e.g. /tmp/repo-scanner-results.sarif)",
)
@click.option(
    "--create-issues/--no-create-issues",
    default=False,
    help="Create GitHub issues for findings",
)
@click.option(
    "--post-comment/--no-post-comment",
    default=False,
    help="Post PR comment with summary",
)
@click.option(
    "--full-scan/--diff-only",
    default=False,
    help="Scan all files vs only changed files",
)
@click.option(
    "--severity",
    "-s",
    default=None,
    help="Override severity threshold (low/medium/high/critical)",
)
@click.pass_context
def scan(
    ctx,
    config,
    output,
    output_json,
    output_sarif,
    create_issues,
    post_comment,
    full_scan,
    severity,
):
    """Scan repository for issues."""
    cfg = Config.from_env_and_file(config)

    if severity:
        cfg.severity_threshold = severity

    _require_api_key(cfg)
    _require_repo(cfg)

    workspace = cfg.workspace or str(Path.cwd())
    console.print(f"[bold]Repo Scanner[/bold] — Scanning {cfg.repo}")
    console.print(
        f"  Model: {cfg.model} | Threshold: {cfg.severity_threshold} | Max files: {cfg.max_files}"
    )

    files: list[FileChange] = []

    if full_scan:
        console.print("  Mode: Full scan")
        files = scan_files(workspace, cfg)
    else:
        console.print("  Mode: Diff-aware scan")
        files = get_changed_files_from_event(cfg.event_path, workspace)
        if not files:
            console.print("  No changed files from event, falling back to full scan")
            files = scan_files(workspace, cfg)

    if not files:
        console.print("[yellow]No files to analyze.[/yellow]")
        sys.exit(0)

    files = [
        f
        for f in files
        if f.path
        and not any(
            p in f.path for p in ["node_modules", ".git", "__pycache__", "venv"]
        )
    ]
    files = files[: cfg.max_files]

    console.print(f"  Analyzing {len(files)} file(s)...")

    all_issues = analyze_files(files, cfg)
    issues = filter_by_threshold(all_issues, cfg)

    console.print(
        f"  Found {len(all_issues)} total issues, {len(issues)} above threshold"
    )

    created_urls = []
    if create_issues and cfg.github_token:
        console.print("  Ensuring labels exist...")
        try:
            ensure_labels(cfg)
            console.print("    Labels ready")
        except Exception as e:
            console.print(f"    [yellow]Warning: Could not ensure labels: {e}[/yellow]")

        existing_titles: set[str] = set()
        try:
            existing_titles = get_existing_issue_titles(cfg)
            console.print(
                f"  Found {len(existing_titles)} existing open issue(s), skipping duplicates"
            )
        except Exception as e:
            console.print(
                f"    [yellow]Warning: Could not fetch existing issues: {e}[/yellow]"
            )

        console.print("  Creating GitHub issues...")
        for issue in issues:
            try:
                # Check for similar duplicates before creating
                title = f"[{issue.severity.upper()}] {issue.title}"
                duplicates = find_duplicate_issues(cfg, title)

                if duplicates:
                    # Close the duplicate issue
                    duplicate_of = duplicates[0]["number"]
                    console.print(
                        f"    Skipping duplicate: {title} (similar to #{duplicate_of})"
                    )
                    # Optionally close the duplicate if it's a new scan finding
                    # close_duplicate_issue(cfg, issue.number, duplicate_of)
                    continue

                url = create_issue(cfg, issue, cfg.sha, existing_titles)
                if url is None:
                    console.print(
                        f"    Skipped (duplicate): [{issue.severity.upper()}] {issue.title}"
                    )
                else:
                    created_urls.append(url)
                    console.print(f"    Created: {url}")
            except Exception as e:
                console.print(f"    [red]Failed to create issue: {e}[/red]")

    if post_comment and cfg.github_token:
        pr_number = get_pr_number_from_event(cfg.event_path)
        if pr_number:
            console.print(f"  Posting PR comment on #{pr_number}...")
            summary = build_pr_summary(issues)
            try:
                url = post_pr_comment(cfg, pr_number, summary)
                console.print(f"    Comment posted: {url}")
            except Exception as e:
                console.print(f"    [red]Failed to post comment: {e}[/red]")

    md_report = generate_markdown_report(issues, cfg.repo, cfg.sha)
    save_report(md_report, output)
    console.print(f"  Report saved to {output}")

    if output_json:
        json_report = generate_json_report(issues, cfg.repo, cfg.sha)
        save_report(json_report, output_json)
        console.print(f"  JSON report saved to {output_json}")

    if output_sarif:
        sarif_report = generate_sarif_report(issues, cfg.repo, cfg.sha)
        save_report(sarif_report, output_sarif)
        console.print(f"  SARIF report saved to {output_sarif}")

    # Send completion notification
    if os.environ.get("EMAIL_NOTIFICATIONS", "false").lower() == "true":
        notify_scan_completed(cfg, len(issues), len(created_urls))

    print_results(issues, created_urls)


@cli.command()
@click.option("--config", "-c", default=None, help="Path to config YAML file")
@click.option("--issue-number", required=True, type=int, help="GitHub issue number to investigate")
@click.option("--replan", is_flag=True, default=False, help="Delete old plan and regenerate")
@click.pass_context
def plan(ctx, config, issue_number, replan):
    """Investigate a GitHub issue and post an AI fix plan as a comment."""
    from github import Github
    from .planner import run_plan, MAX_REPLAN_ATTEMPTS

    cfg = Config.from_env_and_file(config)
    _require_api_key(cfg)
    _require_repo(cfg)

    if not cfg.github_token:
        console.print("[red]Error: No GitHub token. Use GITHUB_TOKEN env var.[/red]")
        sys.exit(1)

    workspace = cfg.workspace or str(Path.cwd())

    g = Github(cfg.github_token)
    repo = g.get_repo(cfg.repo)
    gh_issue = repo.get_issue(issue_number)

    replans = 0

    if replan:
        existing = get_latest_plan_comment(cfg, issue_number)
        if existing:
            replans = existing["replans"] + 1
            if replans >= MAX_REPLAN_ATTEMPTS:
                post_issue_comment(
                    cfg,
                    issue_number,
                    f"⚠️ **Maximum replan attempts reached ({MAX_REPLAN_ATTEMPTS}).**\n\n"
                    f"The AI was unable to generate a satisfactory plan automatically.\n\n"
                    f"To try again, add more detail to the issue — for example:\n"
                    f"- The file path where the bug occurs: `**File:** \\`path/to/file.py:42\\``\n"
                    f"- The exact error message\n"
                    f"- Steps to reproduce\n\n"
                    f"Then add the `plan` label.",
                )
                swap_label(cfg, issue_number, "replan", "needs-manual-review")
                console.print(f"[red]Max replans reached for issue #{issue_number}. Stopping.[/red]")
                return
            delete_plan_comments(cfg, issue_number)
        swap_label(cfg, issue_number, "replan", "plan-ready")

    console.print(f"[bold]Planning fix for issue #{issue_number}:[/bold] {gh_issue.title}")
    console.print(f"  Searching codebase and generating plan (replan #{replans})..." if replans else "  Searching codebase and generating plan...")

    result = run_plan(
        cfg,
        issue_title=gh_issue.title,
        issue_body=gh_issue.body or "",
        workspace=workspace,
        replans=replans,
    )

    if not result["success"]:
        post_issue_comment(
            cfg,
            issue_number,
            f"⚠️ **Could not generate a plan:** {result['error']}\n\n"
            f"Please add more context to the issue (e.g., file path, error message, "
            f"steps to reproduce) and add the `plan` label to try again.",
        )
        swap_label(cfg, issue_number, "plan", "needs-manual-review")
        console.print(f"[red]Planning failed: {result['error']}[/red]")
        sys.exit(1)

    try:
        ensure_labels(cfg)
    except Exception as e:
        console.print(f"[yellow]Warning: Could not ensure labels: {e}[/yellow]")

    comment_url = post_issue_comment(cfg, issue_number, result["plan_comment"])

    remove_label = "replan" if replan else "plan"
    swap_label(cfg, issue_number, remove_label, "plan-ready")

    console.print(f"[green]Plan posted: {comment_url}[/green]")
    console.print(f"  Identified file: {result['file']}:{result['line']}")

    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"plan_file={result['file']}\n")
            f.write(f"plan_line={result['line']}\n")
            f.write(f"comment_url={comment_url}\n")


@cli.command()
@click.option("--config", "-c", default=None, help="Path to config YAML file")
@click.option(
    "--max-fixes",
    default=None,
    type=int,
    help="Maximum number of issues to fix (default from config, fallback 3)",
)
@click.option(
    "--severity",
    "-s",
    default=None,
    help="Only fix issues at or above this severity (low/medium/high/critical)",
)
@click.option(
    "--issue-number",
    default=None,
    type=int,
    help="Fix a specific issue by number instead of fetching all open issues",
)
@click.option(
    "--min-age-days",
    default=0,
    type=int,
    help="Only fix issues older than this many days (0 = no limit)",
)
@click.option(
    "--from-plan",
    is_flag=True,
    default=False,
    help="Read fix details from plan comment instead of issue body",
)
@click.pass_context
def fix(ctx, config, max_fixes, severity, issue_number, min_age_days, from_plan):
    """Auto-fix scanner issues and create PRs."""
    cfg = Config.from_env_and_file(config)

    _require_api_key(cfg)
    _require_repo(cfg)

    if not cfg.github_token:
        console.print(
            "[red]Error: No GitHub token set. Use GITHUB_TOKEN env var.[/red]"
        )
        sys.exit(1)

    workspace = cfg.workspace or str(Path.cwd())

    if from_plan:
        if not issue_number:
            console.print("[red]--from-plan requires --issue-number[/red]")
            sys.exit(1)
        console.print(f"[bold]Repo Scanner Fix (from plan)[/bold] — Issue #{issue_number} in {cfg.repo}")
        result = fix_from_plan_comment(cfg, issue_number, workspace)
        if result.success:
            console.print(f"[green]Fix PR created: {result.pr_url}[/green]")
        else:
            console.print(f"[red]Fix failed: {result.error}[/red]")
        github_output = os.environ.get("GITHUB_OUTPUT", "")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"fixes_created={'1' if result.success else '0'}\n")
                f.write(f"pr_urls={result.pr_url if result.success else ''}\n")
        if not result.success:
            sys.exit(1)
        return

    effective_max = (
        1
        if issue_number is not None
        else (max_fixes if max_fixes is not None else cfg.max_fixes)
    )

    console.print(f"[bold]Repo Scanner Fix[/bold] — Fixing issues in {cfg.repo}")
    console.print(
        f"  Model: {cfg.model} | Max fixes: {effective_max} | Validate: {cfg.validate_fixes}"
    )
    if issue_number:
        console.print(f"  Targeting issue: #{issue_number}")
    if severity:
        console.print(f"  Severity filter: {severity} and above")
    if min_age_days > 0:
        console.print(f"  Age gate: only issues older than {min_age_days} day(s)")

    results: list[FixResult] = run_fixes(
        cfg,
        effective_max,
        severity,
        workspace,
        issue_number=issue_number,
        min_age_days=min_age_days,
        validate_fixes=cfg.validate_fixes,
    )

    if not results:
        console.print("[yellow]No open scanner issues found to fix.[/yellow]")
        return

    table = Table(title="Fix Results")
    table.add_column("Issue #")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("PR / Error")

    for r in results:
        if r.success:
            table.add_row(
                str(r.issue_number),
                r.issue_title,
                "[green]Success[/green]",
                r.pr_url,
            )
        else:
            error_display = r.error
            if r.validation_error:
                error_display = f"Validation failed: {r.validation_error}"
            table.add_row(
                str(r.issue_number),
                r.issue_title,
                "[red]Failed[/red]",
                error_display,
            )

    console.print(table)

    successes = sum(1 for r in results if r.success)
    console.print(f"\n  {successes}/{len(results)} fixes created successfully")

    # Send completion notification
    if os.environ.get("EMAIL_NOTIFICATIONS", "false").lower() == "true":
        notify_fix_completed(cfg, len(results), successes)

    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        pr_urls = ",".join(r.pr_url for r in results if r.success)
        with open(github_output, "a") as f:
            f.write(f"fixes_created={successes}\n")
            f.write(f"pr_urls={pr_urls}\n")


def main():
    cli()


if __name__ == "__main__":
    main()
