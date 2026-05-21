# Repo Scanner

[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-Repo%20Scanner-blue?logo=github)](https://github.com/marketplace/actions/repo-scanner-ai-code-analysis-auto-fix)

AI-powered code analysis GitHub Action powered by [Xiaomi MiMo](https://platform.xiaomimimo.com). Scans your codebase for bugs, security vulnerabilities, code quality issues, and performance problems.

## Features

- **Bug Detection** — Logic errors, null pointer risks, race conditions
- **Security Analysis** — SQL injection, XSS, hardcoded secrets, insecure crypto
- **Code Quality** — Code smells, complexity, missing error handling
- **Performance** — N+1 queries, memory leaks, inefficient algorithms
- **Auto Issues** — Creates GitHub Issues for high-severity findings
- **PR Comments** — Posts review summaries on pull requests
- **Reports** — Generates markdown + JSON reports as artifacts

## Quick Start

### 1. Add the Action to Your Workflow

Create `.github/workflows/repo-scanner.yml` — just a few lines:

```yaml
name: Code Scan

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  scan:
    uses: yaseen-vm/repo_scanner/.github/workflows/reusable-scan.yml@master
    with:
      severity_threshold: medium
    secrets:
      mimo_api_key: ${{ secrets.MIMO_API_KEY }}
```

That's it. The reusable workflow handles checkout, setup, scanning, and report uploads.

### 2. Add Your MiMo API Key as a Secret

1. Get an API key from [platform.xiaomimimo.com](https://platform.xiaomimimo.com)
2. Go to your repo → Settings → Secrets and variables → Actions
3. Add `MIMO_API_KEY` with your MiMo API key
4. (Optional) Add `LLM_MODEL` to use a different model (default: `mimo-v2.5-pro`)

**Available MiMo models:** `mimo-v2.5-pro`, `mimo-v2.5`, `mimo-v2-pro`, `mimo-v2-omni`, `mimo-v2-flash`

### 3. That's It!

The scanner will run on PRs and pushes to main.

## Configuration

### Action Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `mimo_api_key` | (required) | MiMo API key |
| `llm_api_key` | (fallback) | API key if mimo_api_key not set |
| `llm_base_url` | `https://api.xiaomimimo.com/v1` | API endpoint URL |
| `llm_model` | `mimo-v2.5-pro` | Model to use for analysis |
| `severity_threshold` | `medium` | Min severity: `low`, `medium`, `high`, `critical` |
| `max_files` | `50` | Max files per run (cost control) |
| `create_issues` | `false` | Auto-create GitHub Issues |
| `post_comment` | `true` | Post PR review comment |
| `full_scan` | `false` | Scan all files vs only changed |
| `ignore_patterns` | (see below) | Comma-separated glob patterns |
| `config_file` | (empty) | Path to config YAML file |

### Reusable Workflow Inputs

The reusable workflows (`reusable-scan.yml`, `reusable-fix.yml`) accept the same inputs. Call them with `uses:`:

```yaml
# Scan workflow
uses: yaseen-vm/repo_scanner/.github/workflows/reusable-scan.yml@master

# Fix workflow
uses: yaseen-vm/repo_scanner/.github/workflows/reusable-fix.yml@master
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `MIMO_API_KEY` | MiMo API key (primary) |
| `LLM_API_KEY` | API key (fallback alias) |
| `LLM_BASE_URL` | API endpoint (default: `https://api.xiaomimimo.com/v1`) |
| `LLM_MODEL` | Model name (default: `mimo-v2.5-pro`) |
| `GITHUB_TOKEN` | Auto-provided by GitHub Actions |
| `SEVERITY_THRESHOLD` | Min severity level |
| `MAX_FILES` | Max files to analyze |
| `IGNORE_PATTERNS` | Glob patterns to skip |

### Config File

Create a `repo-scanner.yml` in your repo root:

```yaml
model: mimo-v2.5-pro
base_url: https://api.xiaomimimo.com/v1
severity_threshold: high
max_files: 100
ignore_patterns:
  - "**/test/**"
  - "**/vendor/**"
  - "*.test.js"
```

## Auto-Fix

Create `.github/workflows/fix.yml` to auto-fix issues and raise PRs:

```yaml
name: Fix Issues

on:
  workflow_dispatch:
  schedule:
    - cron: '0 9 * * 1'

jobs:
  fix:
    uses: yaseen-vm/repo_scanner/.github/workflows/reusable-fix.yml@master
    with:
      severity_threshold: high
      max_fixes: "3"
    secrets:
      mimo_api_key: ${{ secrets.MIMO_API_KEY }}
```

## CLI Usage

You can also run the scanner locally:

```bash
pip install -r requirements.txt

# Set your MiMo API key
export MIMO_API_KEY="your-mimo-key"

# Run a scan
python -m repo_scanner.main --full-scan --create-issues --severity high
```

### CLI Options

```
Options:
  -c, --config PATH          Path to config YAML file
  -o, --output PATH          Output report path (default: repo-scanner-report.md)
  --output-json PATH         Output JSON report path
  --create-issues            Create GitHub issues for findings
  --no-create-issues         Don't create issues (default)
  --post-comment             Post PR comment with summary
  --no-post-comment          Don't post PR comment
  --full-scan                Scan all files
  --diff-only                Scan only changed files (default)
  -s, --severity TEXT        Override severity threshold
  --help                     Show this message and exit
```

## How It Works

1. **Trigger** — Runs on PR open/update or push to main
2. **File Discovery** — Gets changed files (diff-aware) or scans all files
3. **AI Analysis** — Sends code to LLM with structured analysis prompts
4. **Issue Filtering** — Filters results by severity threshold
5. **GitHub Integration** — Creates Issues and posts PR comments
6. **Reporting** — Saves markdown + JSON reports as artifacts

## Supported Languages

Python, JavaScript, TypeScript, Java, Go, Rust, Ruby, C/C++, C#, PHP, Swift, Kotlin, Scala, Shell, SQL, HTML, CSS, Vue, Svelte, Terraform, YAML, JSON, TOML

## Cost Control

- `max_files` limits files per run
- `ignore_patterns` skips irrelevant files
- `max_file_size` (config) skips large files
- Diff-aware mode only analyzes changed files
- Batches files 5 at a time to reduce API calls

## License

MIT
