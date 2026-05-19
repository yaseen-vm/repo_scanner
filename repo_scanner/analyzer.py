import json
from dataclasses import dataclass, field

from openai import OpenAI
from openai import AuthenticationError, RateLimitError

from .config import Config
from .notifications import notify_token_expired, notify_token_exhausted
from .scanner import FileChange

SYSTEM_PROMPT = """You are an expert code reviewer and security analyst. Analyze the provided code for:

1. **Bugs** - Logic errors, null pointer risks, race conditions, off-by-one errors
2. **Security** - SQL injection, XSS, hardcoded secrets, insecure crypto, path traversal, SSRF
3. **Code Quality** - Code smells, poor naming, high complexity, missing error handling
4. **Performance** - N+1 queries, memory leaks, O(n²) loops, unnecessary allocations

For each issue found, respond with a JSON array of objects:
```json
[
  {
    "file": "path/to/file.py",
    "line": 42,
    "severity": "high",
    "category": "security",
    "title": "Short title",
    "description": "Detailed explanation of the issue",
    "suggestion": "How to fix it"
  }
]
```

Rules:
- Only report real, actionable issues. Do not report style preferences.
- Severity must be one of: low, medium, high, critical
- Category must be one of: bug, security, quality, performance
- If no issues found, return an empty array []
- Respond ONLY with the JSON array, no other text
"""


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


def analyze_files(files: list[FileChange], config: Config) -> list[Issue]:
    if not files:
        return []

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
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=4096,
            )

            content = response.choices[0].message.content or "[]"
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            issues_data = json.loads(content)
            for item in issues_data:
                all_issues.append(
                    Issue(
                        file=item.get("file", ""),
                        line=item.get("line", 0),
                        severity=item.get("severity", "medium"),
                        category=item.get("category", "quality"),
                        title=item.get("title", ""),
                        description=item.get("description", ""),
                        suggestion=item.get("suggestion", ""),
                    )
                )
        except AuthenticationError as e:
            # Token expired or invalid
            error_msg = str(e)
            print(f"Authentication error: {error_msg}")
            notify_token_expired(config, error_msg)
            # Re-raise to stop processing
            raise
        except RateLimitError as e:
            # Rate limit exceeded (quota exhausted)
            error_msg = str(e)
            print(f"Rate limit error: {error_msg}")
            notify_token_exhausted(config, error_msg)
            # Continue processing other batches (might have quota left)
            continue
        except json.JSONDecodeError:
            continue
        except Exception:
            continue

    return all_issues


def filter_by_threshold(issues: list[Issue], config: Config) -> list[Issue]:
    return [i for i in issues if config.meets_threshold(i.severity)]
