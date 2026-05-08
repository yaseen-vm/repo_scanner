import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    api_key: str = ""
    base_url: str = "https://api.xiaomimimo.com/v1"
    model: str = "mimo-v2.5-pro"
    github_token: str = ""
    repo: str = ""
    severity_threshold: str = "medium"
    ignore_patterns: list[str] = field(
        default_factory=lambda: [
            "**/node_modules/**",
            "**/.git/**",
            "**/__pycache__/**",
            "**/venv/**",
            "**/.venv/**",
            "**/dist/**",
            "**/build/**",
            "*.lock",
            "*.min.js",
            "*.min.css",
        ]
    )
    max_files: int = 50
    max_file_size: int = 100_000
    max_fixes: int = 3
    event_name: str = ""
    event_path: str = ""
    sha: str = ""
    ref: str = ""
    workspace: str = ""

    @classmethod
    def from_env_and_file(cls, config_path: str | None = None) -> "Config":
        cfg = cls()

        if config_path and Path(config_path).exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)

        cfg.api_key = os.environ.get(
            "LLM_API_KEY", os.environ.get("MIMO_API_KEY", cfg.api_key)
        )
        cfg.base_url = os.environ.get("LLM_BASE_URL", cfg.base_url)
        cfg.model = os.environ.get("LLM_MODEL", cfg.model)
        cfg.github_token = os.environ.get("GITHUB_TOKEN", cfg.github_token)
        cfg.repo = os.environ.get("GITHUB_REPOSITORY", cfg.repo)
        cfg.severity_threshold = os.environ.get(
            "SEVERITY_THRESHOLD", cfg.severity_threshold
        )
        cfg.max_files = int(os.environ.get("MAX_FILES", str(cfg.max_files)))
        cfg.event_name = os.environ.get("GITHUB_EVENT_NAME", cfg.event_name)
        cfg.event_path = os.environ.get("GITHUB_EVENT_PATH", cfg.event_path)
        cfg.sha = os.environ.get("GITHUB_SHA", cfg.sha)
        cfg.ref = os.environ.get("GITHUB_REF", cfg.ref)
        cfg.workspace = os.environ.get("GITHUB_WORKSPACE", cfg.workspace)

        ignore_env = os.environ.get("IGNORE_PATTERNS")
        if ignore_env:
            cfg.ignore_patterns = [p.strip() for p in ignore_env.split(",")]

        return cfg

    @property
    def severity_rank(self) -> dict[str, int]:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}

    def meets_threshold(self, severity: str) -> bool:
        return self.severity_rank.get(severity, 0) >= self.severity_rank.get(
            self.severity_threshold, 2
        )
