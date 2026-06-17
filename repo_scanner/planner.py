import re
from pathlib import Path

from openai import OpenAI

from .config import Config

PLAN_MARKER = "repo-scanner-plan-v1"
MAX_REPLAN_ATTEMPTS = 3

PLAN_SYSTEM_PROMPT = """You are a senior software engineer investigating a GitHub issue report.
You will be given the issue title, issue body, and contents of relevant files from the codebase.

Your task:
1. Identify which file and line number contains the root cause
2. Explain the root cause clearly and concisely
3. Propose a specific, actionable fix

Respond in EXACTLY this format (no markdown fences, no extra text before or after):

**Affected File:** `path/to/file.py`
**Line:** <line number, or 0 if unknown>
**Root Cause:** <one sentence describing the problem>
**Proposed Fix:** <specific change to make>

## Reasoning
<2-3 paragraphs tracing how you found the issue in the codebase>

If you cannot identify the affected file with confidence, set Affected File to `UNKNOWN` and explain why in Reasoning."""

_STOP_WORDS = {
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "for", "of", "and",
    "or", "but", "not", "with", "this", "that", "from", "be", "are", "was",
    "were", "been", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "i", "we", "you", "he", "she",
    "they", "my", "our", "your", "their", "its", "when", "where", "what",
    "which", "who", "how", "why", "if", "then", "else", "than", "so", "as",
    "by", "up", "out", "about", "into", "through", "after", "before", "all",
    "any", "each", "more", "also", "just", "like", "get", "use", "make",
    "error", "issue", "bug", "problem", "fix", "need", "want", "using", "used",
}

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", "coverage", ".tox", "_repo_scanner",
}

_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".swift", ".kt", ".scala",
    ".sh", ".bash", ".sql", ".html", ".css", ".vue", ".svelte",
}


def extract_keywords(title: str, body: str) -> list[str]:
    text = f"{title} {body}"
    keywords: set[str] = set()

    for term in re.findall(r'["\']([^"\']{2,40})["\']', text):
        keywords.add(term.strip())
    for term in re.findall(r'`([^`]{2,60})`', text):
        keywords.add(term.strip())
    for term in re.findall(r'[\w/\\.-]+\.\w{2,4}', text):
        keywords.add(term.strip())
    for word in re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b', text):
        if word.lower() not in _STOP_WORDS:
            keywords.add(word)

    return list(keywords)[:30]


def _score_file(file_path: Path, keywords: list[str]) -> int:
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    content_lower = content.lower()
    score = 0
    for kw in keywords:
        count = content_lower.count(kw.lower())
        if count > 0:
            score += min(count, 5)
    return score


def search_codebase(workspace: str, keywords: list[str], max_files: int = 10) -> list[tuple[str, int, str]]:
    """Returns list of (rel_path, score, content) sorted by relevance desc."""
    workspace_path = Path(workspace)
    candidates: list[tuple[Path, int]] = []

    for file_path in workspace_path.rglob("*"):
        if not file_path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in file_path.parts):
            continue
        if file_path.suffix not in _CODE_EXTENSIONS:
            continue
        try:
            if file_path.stat().st_size > 200_000:
                continue
        except OSError:
            continue

        score = _score_file(file_path, keywords)
        if score > 0:
            candidates.append((file_path, score))

    candidates.sort(key=lambda x: x[1], reverse=True)

    result: list[tuple[str, int, str]] = []
    for file_path, score in candidates[:max_files]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            rel_path = str(file_path.relative_to(workspace_path))
            result.append((rel_path, score, content))
        except OSError:
            continue

    return result


def generate_plan(
    config: Config,
    issue_title: str,
    issue_body: str,
    candidates: list[tuple[str, int, str]],
) -> str:
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    file_sections = []
    for rel_path, _score, content in candidates:
        if len(content) > 6000:
            content = content[:6000] + "\n... [truncated]"
        file_sections.append(f"### `{rel_path}`\n```\n{content}\n```")

    files_text = "\n\n".join(file_sections) if file_sections else "No relevant files found in the codebase."

    user_message = (
        f"## Issue Title\n{issue_title}\n\n"
        f"## Issue Description\n{issue_body or '(no description provided)'}\n\n"
        f"## Relevant Files\n\n{files_text}"
    )

    response = client.chat.completions.create(
        model=config.model,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
        max_tokens=2048,
    )

    return (response.choices[0].message.content or "").strip()


def _extract_plan_fields(plan_text: str) -> tuple[str, int]:
    file_match = re.search(r'\*\*Affected File:\*\*\s*`([^`]+)`', plan_text)
    line_match = re.search(r'\*\*Line:\*\*\s*(\d+)', plan_text)
    file_path = file_match.group(1) if file_match else "UNKNOWN"
    line = int(line_match.group(1)) if line_match else 0
    return file_path, line


def extract_fix_fields(plan_comment_body: str) -> tuple[str, str]:
    """Pull root cause and proposed fix text out of a plan comment body."""
    root_cause_match = re.search(
        r'\*\*Root Cause:\*\*\s*(.+?)(?=\n\*\*|\n##|$)', plan_comment_body, re.DOTALL
    )
    proposed_fix_match = re.search(
        r'\*\*Proposed Fix:\*\*\s*(.+?)(?=\n\*\*|\n##|$)', plan_comment_body, re.DOTALL
    )
    reasoning_match = re.search(
        r'## Reasoning\s*(.+?)(?=\n---|\n<!--|$)', plan_comment_body, re.DOTALL
    )

    description = (root_cause_match.group(1).strip() if root_cause_match else "")
    if reasoning_match:
        description += "\n\n" + reasoning_match.group(1).strip()

    suggestion = proposed_fix_match.group(1).strip() if proposed_fix_match else ""
    return description, suggestion


def build_plan_comment(plan_text: str, replans: int, file_path: str, line: int) -> str:
    marker = f"<!-- {PLAN_MARKER} | replans: {replans} | file: {file_path} | line: {line} -->"
    return f"## AI Fix Plan\n\n{plan_text}\n\n---\n{marker}"


def parse_plan_comment(body: str) -> dict | None:
    pattern = rf"<!-- {re.escape(PLAN_MARKER)} \| replans: (\d+) \| file: ([^|]+) \| line: (\d+) -->"
    m = re.search(pattern, body)
    if not m:
        return None
    return {
        "replans": int(m.group(1)),
        "file": m.group(2).strip(),
        "line": int(m.group(3)),
    }


def run_plan(
    config: Config,
    issue_title: str,
    issue_body: str,
    workspace: str,
    replans: int = 0,
) -> dict:
    """
    Investigate the issue and generate a plan comment.
    Returns: {success, plan_comment, file, line, error}
    """
    keywords = extract_keywords(issue_title, issue_body)
    if not keywords:
        return {
            "success": False,
            "error": "Could not extract meaningful keywords from issue.",
            "plan_comment": "",
            "file": "",
            "line": 0,
        }

    candidates = search_codebase(workspace, keywords)
    plan_text = generate_plan(config, issue_title, issue_body, candidates)
    file_path, line = _extract_plan_fields(plan_text)
    plan_comment = build_plan_comment(plan_text, replans, file_path, line)

    return {
        "success": True,
        "plan_comment": plan_comment,
        "file": file_path,
        "line": line,
        "error": "",
    }
