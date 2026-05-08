# AGENTS.md

## What this is

Single-package Python CLI + GitHub Action that uses an OpenAI-compatible LLM (Xiaomi MiMo) to scan code for bugs, security issues, and quality problems. It can auto-create GitHub issues and raise fix PRs.

## Run locally

```
pip install -r requirements.txt

# Scan for issues
python -m repo_scanner.main scan --full-scan --severity high

# Auto-fix open scanner issues and create PRs
python -m repo_scanner.main fix --max-fixes 3 --severity high
```

Entry point: `repo_scanner/main.py` (Click CLI group with `scan` and `fix` subcommands). Running `repo_scanner.main` with no subcommand defaults to `scan`. Env vars `LLM_API_KEY` or `MIMO_API_KEY` are required. See `config.example.yml` for all options.

## Project structure

```
repo_scanner/
  main.py          # CLI entry (click group: scan, fix)
  config.py        # Config from env vars + YAML
  scanner.py       # File discovery, diff-aware or full scan
  analyzer.py      # LLM analysis, batches files 5 at a time
  fixer.py         # Auto-fix: fetch issues, generate fixes, create PRs
  github_client.py # Issues, labels, PR comments via PyGithub
  reporter.py      # Markdown + JSON report generation
action.yml         # GitHub Action definition (composite)
```

## Key facts

- **Python 3.10+**, deps in both `pyproject.toml` and `requirements.txt` (keep in sync).
- **No tests, no linter config, no type checker** exist. `.ruff_cache/` is present but no ruff config is defined — don't assume ruff is configured.
- **Default branch is `master`**, not `main`.
- **No lockfile** is committed.
- **`.gitignore` excludes all `*.md` except `README.md` and `AGENTS.md`.** If you add other markdown files, update `.gitignore`.
- **LLM batching**: files are sent to the analyzer in batches of 5 (`BATCH_SIZE` in `analyzer.py`). Costs can be controlled via `MAX_FILES` and `IGNORE_PATTERNS` in config.
- **Scanner limitation**: `get_changed_files_from_event()` in `scanner.py` only handles push events. For PR events, it falls back to full scan mode.
- **action.yml** references `yaseen-vm/repo_scanner@master` — it checks out the repo itself to run.
- **Severity levels**: low(1) < medium(2) < high(3) < critical(4). Threshold filtering in `analyzer.py:filter_by_threshold()`.
- **Label auto-creation**: `github_client.py` creates 9 labels (severity + category + `repo-scanner`) on first run.
- **Fix flow**: `fixer.py` fetches open issues labeled `repo-scanner`, generates patches via LLM, creates branches `fix/<num>-<slug>`, pushes, and opens PRs with `Closes #N`. One PR per issue. Git ops use subprocess.
- **CLI is a click group**: `scan` and `fix` are subcommands. No subcommand defaults to `scan`.

## Style notes

- Dataclasses used throughout (`Config`, `FileChange`, `Issue`, `FixResult`), not Pydantic.
- `rich` for terminal output (Console, Table), `click` for CLI.
- No async code anywhere — everything is synchronous.
- JSON parsing in `analyzer.py` handles LLM responses wrapped in code fences.
- `fixer.py` uses `subprocess.run(["git", ...])` for all git operations — no gitpython dependency.
