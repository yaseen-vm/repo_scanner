import re
from pathlib import Path

from openai import OpenAI

from .config import Config

PLAN_MARKER = "repo-scanner-plan-v1"
MAX_REPLAN_ATTEMPTS = 3

PLAN_SYSTEM_PROMPT = """You are an elite forward-deployed engineer and imagineer embedded directly inside this codebase. You have been dropped on-site to solve a real problem that is blocking a real team right now.

You operate with two simultaneous mindsets:

FORWARD-DEPLOYED ENGINEER
You are on the ground. You read everything — not just the obvious files. You do not wait for a perfect specification. You read between the lines, infer intent from context, and ship a solution. You handle every edge case, not just the happy path. You think about what breaks at 3am, what the junior engineer will misuse, what the input validation is missing, what happens when the data is null, empty, a duplicate, or malformed. You own the outcome entirely. If something needs to be created from scratch because it does not exist yet, you create it — you name the file, design the structure, define the interface. You treat the person who raised this issue like a customer standing in front of you who cannot ship until this is resolved.

IMAGINEER
You combine imagination with engineering precision. You do not only see what IS in the codebase — you see what SHOULD BE. You look at the existing files, understand their conventions, their naming patterns, their style, and you extend the system naturally. You design what is missing so it feels like it was always there. You think about the full user journey and the full data flow, not just the single line that triggered the report.

---

YOUR TASK
Read the issue title and body. Read every file in the codebase provided. Then produce a complete, decisive, actionable fix plan.

The issue will fall into one of these categories — you figure out which:
- Bug in existing code → trace it to the exact line, explain why it is wrong, describe the precise fix
- Missing feature or capability → decide what file to create, what to name it, design its full implementation
- Incomplete placeholder → complete it properly with all required logic
- Ambiguous complaint → infer the most likely root cause from the codebase and address it directly

---

NON-NEGOTIABLE RULES
1. You ALWAYS produce a complete response. Returning empty is not an option. If you are uncertain, state your assumption and proceed with the most reasonable interpretation.
2. You ALWAYS identify a specific file — an existing one to modify, or a new one to create with a concrete filename. UNKNOWN is only acceptable if you have exhausted all reasoning and genuinely cannot determine the file.
3. Your Proposed Fix MUST be detailed enough that a junior engineer can implement it without asking a single follow-up question. Include function names, key logic, what to import, edge cases to handle.
4. You account for edge cases in the fix — null inputs, empty collections, duplicate entries, concurrent access, invalid states.
5. You respect the existing codebase's style, naming conventions, and patterns exactly.
6. You MUST output all four header fields and the Reasoning section. No exceptions.

---

RESPOND IN EXACTLY THIS FORMAT (no markdown fences, no preamble, no sign-off):

**Affected File:** `path/to/file.ext`
**Line:** <line number where the change starts, or 0 for a new file>
**Root Cause:** <one sharp sentence — what is broken or absent and why it matters>
**Proposed Fix:** <complete implementation description — function signatures, key logic, edge cases, imports needed>

## Reasoning
<paragraph 1: how you read the codebase and what you found>
<paragraph 2: why this is the correct file and approach>
<paragraph 3: edge cases and failure modes you are accounting for>"""

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


def _extract_imports(content: str, file_path: Path) -> list[str]:
    """GAP 2: Extract imported module names from a source file."""
    imports: list[str] = []
    ext = file_path.suffix.lower()
    if ext == ".py":
        for m in re.finditer(r'^from\s+(\S+)\s+import', content, re.MULTILINE):
            imports.append(m.group(1))
        for m in re.finditer(r'^import\s+(\S+)', content, re.MULTILINE):
            imports.append(m.group(1).split(".")[0])
    elif ext in {".js", ".ts", ".jsx", ".tsx", ".mjs"}:
        for m in re.finditer(r'''from\s+['"]([^'"]+)['"]''', content):
            imports.append(m.group(1))
        for m in re.finditer(r'''require\s*\(\s*['"]([^'"]+)['"]\s*\)''', content):
            imports.append(m.group(1))
    return imports


def _resolve_import_to_file(module: str, workspace_path: Path) -> Path | None:
    """GAP 2: Try to resolve an import module name to an actual file in workspace."""
    # Convert dots/slashes to path separators
    candidate_base = module.replace(".", "/").replace("\\", "/").lstrip("/")
    # Try various extensions
    for ext in (".py", ".ts", ".js", ".tsx", ".jsx"):
        candidate = workspace_path / (candidate_base + ext)
        if candidate.exists() and candidate.is_file():
            return candidate
    # Try as directory with __init__.py (Python packages)
    candidate = workspace_path / candidate_base / "__init__.py"
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def search_codebase(workspace: str, keywords: list[str], max_files: int = 10, score_threshold: int = 1) -> list[tuple[str, int, str]]:
    """Returns list of (rel_path, score, content) sorted by relevance desc.
    GAP 2: Also performs a second pass of import-graph traversal to include
    imported files that may not be keyword-rich themselves."""
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

        score = _score_file(file_path, keywords) if keywords else 1
        if score >= score_threshold:
            candidates.append((file_path, score))

    candidates.sort(key=lambda x: x[1], reverse=True)

    # GAP 2: Import-graph traversal — include files imported by top candidates
    candidate_paths = {fp for fp, _ in candidates}
    max_importer_score = candidates[0][1] if candidates else 1
    import_additions: list[tuple[Path, int]] = []

    for file_path, score in candidates[:max_files]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for module_name in _extract_imports(content, file_path):
            resolved = _resolve_import_to_file(module_name, workspace_path)
            if resolved is None:
                continue
            if resolved in candidate_paths:
                continue
            # Skip files already queued for addition
            if any(fp == resolved for fp, _ in import_additions):
                continue
            # Add with half the max importer score so it ranks after keyword matches
            import_score = max(1, max_importer_score // 2)
            import_additions.append((resolved, import_score))
            candidate_paths.add(resolved)

    all_candidates = candidates + import_additions
    all_candidates.sort(key=lambda x: x[1], reverse=True)

    # Cap final list at 12
    result: list[tuple[str, int, str]] = []
    for file_path, score in all_candidates[:12]:
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
    workspace: str = "",
) -> str:
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    def _build_files_text(file_list: list[tuple[str, int, str]]) -> str:
        sections = []
        for rel_path, _score, content in file_list:
            if len(content) > 6000:
                content = content[:6000] + "\n... [truncated]"
            sections.append(f"### `{rel_path}`\n```\n{content}\n```")
        return "\n\n".join(sections) if sections else "No files found in the codebase."

    def _call(files_text: str, temperature: float) -> str:
        user_message = (
            f"## Issue Title\n{issue_title}\n\n"
            f"## Issue Description\n{issue_body or '(no description provided)'}\n\n"
            f"## Codebase Files\n\n{files_text}"
        )
        response = client.chat.completions.create(
            model=config.model,
            messages=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=3000,
        )
        return (response.choices[0].message.content or "").strip()

    # Attempt 1 — keyword-scored candidates
    files_text = _build_files_text(candidates)
    result = _call(files_text, temperature=0.2)
    if result:
        return result

    # Attempt 2 — include ALL code files in the repo (no keyword filter)
    # This handles feature requests where no existing file matches the keywords
    if workspace:
        all_files = search_codebase(workspace, keywords=[], max_files=15, score_threshold=0)
        if all_files:
            files_text = _build_files_text(all_files)
            result = _call(files_text, temperature=0.3)
            if result:
                return result

    # Attempt 3 — issue only, no files, higher temperature, let the model decide from scratch
    result = _call("No existing files were found. Design and create what is needed from scratch.", temperature=0.5)
    return result


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
    plan_text = generate_plan(config, issue_title, issue_body, candidates, workspace=workspace)

    if not plan_text or not plan_text.strip():
        return {
            "success": False,
            "error": "The AI returned an empty response. The model may not have understood the request. Try adding more detail to the issue body (e.g. the target file path, error message, or exact requirements).",
            "plan_comment": "",
            "file": "",
            "line": 0,
        }

    file_path, line = _extract_plan_fields(plan_text)
    plan_comment = build_plan_comment(plan_text, replans, file_path, line)

    return {
        "success": True,
        "plan_comment": plan_comment,
        "file": file_path,
        "line": line,
        "error": "",
    }
