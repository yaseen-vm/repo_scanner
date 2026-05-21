import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from openai import OpenAI
from openai import AuthenticationError, RateLimitError

from .config import Config
from .notifications import notify_token_expired, notify_token_exhausted
from .scanner import FileChange

RESPONSE_FORMAT = """For each issue found, respond with a JSON array of objects:
```json
[
  {
    "file": "path/to/file.py",
    "line": 42,
    "severity": "high",
    "category": "%s",
    "title": "Short title",
    "description": "Detailed explanation of the issue",
    "suggestion": "How to fix it"
  }
]
```

Rules:
- Only report real, actionable issues. Do not report style preferences.
- Severity must be one of: low, medium, high, critical
- If no issues found, return an empty array []
- Respond ONLY with the JSON array, no other text
"""

SECURITY_PROMPT = (
    "You are a security-focused code reviewer. Look ONLY for security vulnerabilities: "
    "SQL injection, XSS, hardcoded secrets, insecure crypto, path traversal, SSRF, "
    "authentication bypass, insecure deserialization, open redirects, command injection, "
    "XXE, prototype pollution, mass assignment.\n\n" + (RESPONSE_FORMAT % "security")
)

BUG_PROMPT = (
    "You are an expert at finding logic errors and runtime bugs: null pointer dereferences, "
    "off-by-one errors, race conditions, incorrect error handling, unreachable code, "
    "type mismatches, unhandled promise rejections, resource leaks, infinite loops, "
    "incorrect boundary checks.\n\n" + (RESPONSE_FORMAT % "bug")
)

PERFORMANCE_PROMPT = (
    "You are a performance engineer. Look ONLY for: N+1 queries, memory leaks, "
    "O(n\u00b2) algorithms where O(n) exists, unnecessary allocations, blocking I/O in async context, "
    "missing indexes, redundant computations, excessive DOM manipulation, "
    "unbounded caches, synchronous file operations in hot paths.\n\n"
    + (RESPONSE_FORMAT % "performance")
)

QUALITY_PROMPT = (
    "You are a senior engineer focused on maintainability. Look ONLY for: high cyclomatic complexity, "
    "magic numbers, misleading variable names, deep nesting, missing error handling at boundaries, "
    "god functions, dead code, copy-paste duplication, violated single responsibility principle, "
    "missing input validation.\n\n" + (RESPONSE_FORMAT % "quality")
)

CATEGORY_PROMPTS: dict[str, str] = {
    "security": SECURITY_PROMPT,
    "bug": BUG_PROMPT,
    "performance": PERFORMANCE_PROMPT,
    "quality": QUALITY_PROMPT,
}

SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class Issue:
    file: str
    line: int
    severity: str
    category: str
    title: str
    description: str
    suggestion: str


def build_file_prompt(file_change: FileChange) -> str:
    parts = [f"## File: `{file_change.path}` (Language: {file_change.language})"]
    if file_change.diff:
        parts.append(f"### Diff:\n```diff\n{file_change.diff}\n```")
    parts.append(
        f"### Full Content:\n```{file_change.language}\n{file_change.content}\n```"
    )
    return "\n".join(parts)


def _parse_issues(content: str, default_category: str) -> list[Issue]:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    issues_data = json.loads(content)
    issues = []
    for item in issues_data:
        issues.append(
            Issue(
                file=item.get("file", ""),
                line=item.get("line", 0),
                severity=item.get("severity", "medium"),
                category=item.get("category", default_category),
                title=item.get("title", ""),
                description=item.get("description", ""),
                suggestion=item.get("suggestion", ""),
            )
        )
    return issues


def _analyze_category(
    files: list[FileChange],
    config: Config,
    system_prompt: str,
    category: str,
) -> list[Issue]:
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    all_issues: list[Issue] = []
    batch_size = 5

    for i in range(0, len(files), batch_size):
        batch = files[i : i + batch_size]
        user_message = "\n\n---\n\n".join(build_file_prompt(f) for f in batch)

        try:
            response = client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=4096,
            )

            content = response.choices[0].message.content or "[]"
            all_issues.extend(_parse_issues(content, category))
        except AuthenticationError as e:
            error_msg = str(e)
            print(f"Authentication error ({category}): {error_msg}")
            notify_token_expired(config, error_msg)
            raise
        except RateLimitError as e:
            error_msg = str(e)
            print(f"Rate limit error ({category}): {error_msg}")
            notify_token_exhausted(config, error_msg)
            continue
        except json.JSONDecodeError:
            continue
        except Exception:
            continue

    return all_issues


def _deduplicate(issues: list[Issue]) -> list[Issue]:
    seen: dict[tuple[str, int], Issue] = {}
    for issue in issues:
        key = (issue.file, issue.line)
        if key in seen:
            existing = seen[key]
            existing_rank = SEVERITY_RANK.get(existing.severity, 0)
            new_rank = SEVERITY_RANK.get(issue.severity, 0)
            if new_rank > existing_rank:
                issue.description = f"{issue.description}\n\n{existing.description}"
                seen[key] = issue
            elif new_rank == existing_rank and issue.category != existing.category:
                existing.description = f"{existing.description}\n\n{issue.description}"
                if issue.category not in existing.category:
                    existing.category = f"{existing.category},{issue.category}"
            else:
                existing.description = f"{existing.description}\n\n{issue.description}"
        else:
            seen[key] = issue
    return list(seen.values())


def analyze_files(files: list[FileChange], config: Config) -> list[Issue]:
    if not files:
        return []

    active_categories = {
        k: v for k, v in CATEGORY_PROMPTS.items() if k in config.categories
    }
    if not active_categories:
        active_categories = CATEGORY_PROMPTS

    all_issues: list[Issue] = []

    with ThreadPoolExecutor(max_workers=len(active_categories)) as executor:
        futures = {
            executor.submit(
                _analyze_category, files, config, prompt, category
            ): category
            for category, prompt in active_categories.items()
        }
        for future in as_completed(futures):
            category = futures[future]
            try:
                result = future.result()
                all_issues.extend(result)
            except Exception:
                print(f"Category '{category}' analysis failed")

    return _deduplicate(all_issues)


def filter_by_threshold(issues: list[Issue], config: Config) -> list[Issue]:
    return [i for i in issues if config.meets_threshold(i.severity)]
