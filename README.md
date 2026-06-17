# Repo Scanner

AI-powered code analysis GitHub Action powered by [Xiaomi MiMo](https://platform.xiaomimimo.com). Scans your codebase for bugs, security vulnerabilities, code quality issues, and performance problems — and can automatically fix them.

## Features

- **Bug Detection** — Logic errors, null pointer risks, race conditions
- **Security Analysis** — SQL injection, XSS, hardcoded secrets, insecure crypto
- **Code Quality** — Code smells, complexity, missing error handling
- **Performance** — N+1 queries, memory leaks, inefficient algorithms
- **Auto Issues** — Creates GitHub Issues for high-severity findings
- **PR Comments** — Posts review summaries on pull requests
- **Reports** — Generates markdown + JSON reports as artifacts
- **Auto-Fix** — Fixes scanner-created issues and opens PRs automatically
- **Plan → Approve → Fix** — AI investigates any freeform issue, proposes a fix plan, and waits for your approval before touching code

---

## Quick Start

### 1. Add the Scan Workflow

Create `.github/workflows/repo-scanner.yml`:

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

### 2. Add Your MiMo API Key

1. Get an API key from [platform.xiaomimimo.com](https://platform.xiaomimimo.com)
2. Go to your repo → **Settings → Secrets and variables → Actions**
3. Add `MIMO_API_KEY` with your key
4. (Optional) Add `LLM_MODEL` to change the model (default: `mimo-v2.5-pro`)

**Available models:** `mimo-v2.5-pro`, `mimo-v2.5`, `mimo-v2-pro`, `mimo-v2-omni`, `mimo-v2-flash`

That's it. The scanner runs on PRs and pushes to main.

---

## How Scanning Works

1. **Trigger** — Runs on PR open/update or push to main
2. **File Discovery** — Gets changed files (diff-aware) or all files
3. **AI Analysis** — Sends code to MiMo in batches of 5 files
4. **Issue Filtering** — Filters results by severity threshold
5. **GitHub Integration** — Creates Issues (on push) and posts PR comments (on PRs)
6. **Reporting** — Saves markdown + JSON reports as artifacts

---

## Auto-Fix for Scanner Issues

The scanner can automatically fix issues it creates. Add `.github/workflows/fix.yml`:

```yaml
name: Fix Issues

on:
  workflow_dispatch:
  schedule:
    - cron: '0 9 * * 1'  # Every Monday at 9am UTC

jobs:
  fix:
    uses: yaseen-vm/repo_scanner/.github/workflows/reusable-fix.yml@master
    with:
      severity_threshold: high
      max_fixes: "3"
    secrets:
      mimo_api_key: ${{ secrets.MIMO_API_KEY }}
```

This reads open GitHub Issues that the scanner created, generates code fixes, and opens PRs — one per issue.

### Manual Approval per Issue

Instead of running fixes on a schedule, you can approve them one at a time using the `auto-fix-approved` label.

Add `.github/workflows/fix-on-label.yml` (already included in this repo) and:

1. Open any issue the scanner created
2. Add the label **`auto-fix-approved`**
3. The fixer runs for that one issue and comments the PR link

---

## Plan → Approve → Fix (Freeform Issues)

This is the most powerful workflow. It lets you raise **any GitHub issue in plain English** — no special format needed — and have the AI investigate your codebase, propose a fix, and wait for your approval before writing a single line of code.

### Setup

Copy these two workflow files into your repo's `.github/workflows/`:

**`.github/workflows/plan-on-label.yml`**
```yaml
name: AI Fix Plan on Label

on:
  issues:
    types: [labeled]

permissions:
  contents: read
  issues: write

jobs:
  plan:
    if: |
      (github.event.label.name == 'plan' || github.event.label.name == 'replan') &&
      github.event.issue.state == 'open'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Generate AI Fix Plan
        uses: yaseen-vm/repo_scanner@master
        with:
          mimo_api_key: ${{ secrets.MIMO_API_KEY }}
          mode: plan
          issue_number: ${{ github.event.issue.number }}
          replan: ${{ github.event.label.name == 'replan' }}
```

**`.github/workflows/approve-on-label.yml`**
```yaml
name: Apply Fix on Approval Label

on:
  issues:
    types: [labeled]

permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  fix-from-plan:
    if: |
      github.event.label.name == 'approved' &&
      github.event.issue.state == 'open' &&
      contains(toJson(github.event.issue.labels), 'plan-ready')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Configure Git
        run: |
          git config user.name "repo-scanner[bot]"
          git config user.email "repo-scanner[bot]@users.noreply.github.com"
      - name: Apply Fix from Plan
        uses: yaseen-vm/repo_scanner@master
        id: fix
        with:
          mimo_api_key: ${{ secrets.MIMO_API_KEY }}
          mode: fix
          issue_number: ${{ github.event.issue.number }}
          from_plan: "true"
      - name: Post result
        uses: actions/github-script@v7
        if: always()
        with:
          script: |
            const issue = context.payload.issue.number;
            const prUrls = '${{ steps.fix.outputs.pr_urls }}';
            if (prUrls) {
              await github.rest.issues.createComment({
                owner: context.repo.owner, repo: context.repo.repo,
                issue_number: issue,
                body: `🤖 Fix PR created: ${prUrls}\n\nReview and merge to close this issue.`
              });
            }
```

### The Workflow Step by Step

```
1. You open a GitHub Issue in plain English
   e.g. "Login page crashes when email field is left empty"

2. Add the label: plan
   ↓
   AI searches the codebase for relevant files
   AI reads and reasons about the code
   AI posts a comment with:
     • Which file is affected (with line number)
     • Root cause explanation
     • Proposed fix
   Label swaps to: plan-ready

3. You read the plan comment and decide:

   ┌─────────────────────┬───────────────────────────────┐
   │ Add label: approved │ Add label: replan             │
   ├─────────────────────┼───────────────────────────────┤
   │ AI generates the    │ AI deletes the old plan and   │
   │ actual code fix     │ generates a new one           │
   │ Creates a branch    │ Label swaps back to plan-ready│
   │ Commits the fix     │ You can replan up to 3 times  │
   │ Opens a PR          │                               │
   │ Comments PR link    │                               │
   └─────────────────────┴───────────────────────────────┘
```

### Labels Used

These labels are created automatically the first time the scanner runs:

| Label | Added by | Meaning |
|---|---|---|
| `plan` | You | Trigger AI investigation |
| `plan-ready` | AI | Plan comment has been posted |
| `approved` | You | Approve the plan and trigger the fix |
| `replan` | You | Reject the plan and request a new one |
| `fix-in-progress` | AI | Fix is being applied |
| `needs-manual-review` | AI | AI couldn't find the file after 3 replans |

### What the Plan Comment Looks Like

```
## AI Fix Plan

**Affected File:** `src/auth/login.py`
**Line:** 87
**Root Cause:** KeyError raised when the email key is absent from the POST body
**Proposed Fix:** Replace `request.form["email"]` with `request.form.get("email", "")`
and add a validation check before proceeding

## Reasoning
Traced the crash through the /login route handler in src/auth/login.py.
The form handler at line 87 accesses request.form["email"] directly without
checking if the key exists...
```

### Tips

- The more detail in the issue body, the better the plan. Include error messages, steps to reproduce, and any file paths you already know.
- If the AI picks the wrong file, add `replan` to try again (up to 3 times).
- After 3 failed replans, the AI adds `needs-manual-review` and stops. Add the correct file path manually to the issue body, then add `plan` to restart.

---

## Configuration

### Action Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `mimo_api_key` | (required) | MiMo API key |
| `llm_api_key` | (fallback) | API key if mimo_api_key not set |
| `llm_base_url` | `https://api.xiaomimimo.com/v1` | API endpoint URL |
| `llm_model` | `mimo-v2.5-pro` | Model to use |
| `severity_threshold` | `medium` | Min severity: `low`, `medium`, `high`, `critical` |
| `max_files` | `50` | Max files per scan (cost control) |
| `create_issues` | `false` | Auto-create GitHub Issues |
| `post_comment` | `true` | Post PR review comment |
| `full_scan` | `false` | Scan all files vs only changed |
| `ignore_patterns` | (see below) | Comma-separated glob patterns |
| `config_file` | (empty) | Path to config YAML file |
| `mode` | `scan` | Action mode: `scan`, `fix`, or `plan` |
| `max_fixes` | `3` | Max issues to fix per run (fix mode) |
| `issue_number` | (empty) | Target a specific issue (fix/plan mode) |
| `min_age_days` | `0` | Only fix issues older than N days |
| `replan` | `false` | Regenerate plan, deleting the old one (plan mode) |
| `from_plan` | `false` | Read fix from plan comment instead of issue body (fix mode) |

### Reusable Workflow Inputs

```yaml
# Scan
uses: yaseen-vm/repo_scanner/.github/workflows/reusable-scan.yml@master

# Fix (scanner issues)
uses: yaseen-vm/repo_scanner/.github/workflows/reusable-fix.yml@master
```

### Config File

Create `repo-scanner.yml` in your repo root:

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

---

## CLI Usage

Run the scanner locally:

```bash
pip install -r requirements.txt
export MIMO_API_KEY="your-mimo-key"
export GITHUB_REPOSITORY="owner/repo"
export GITHUB_TOKEN="your-github-token"

# Scan
python -m repo_scanner.main scan --full-scan --create-issues --severity high

# Fix scanner issues
python -m repo_scanner.main fix --max-fixes 3 --severity high

# Fix a specific scanner issue
python -m repo_scanner.main fix --issue-number 42

# Generate a plan for a freeform issue
python -m repo_scanner.main plan --issue-number 42

# Regenerate the plan
python -m repo_scanner.main plan --issue-number 42 --replan

# Apply the fix from a plan comment
python -m repo_scanner.main fix --issue-number 42 --from-plan
```

---

## Supported Languages

Python, JavaScript, TypeScript, Java, Go, Rust, Ruby, C/C++, C#, PHP, Swift, Kotlin, Scala, Shell, SQL, HTML, CSS, Vue, Svelte

---

## Cost Control

- `max_files` limits files per run
- `ignore_patterns` skips irrelevant files
- `max_file_size` (config) skips large files
- Diff-aware mode only analyzes changed files
- Files sent in batches of 5 to reduce API calls
- Plan mode caps codebase search at 10 most relevant files

---

## License

MIT
