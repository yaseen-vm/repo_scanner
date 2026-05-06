#!/bin/bash
set -e

# Parse action inputs
ARGS=()

while [[ $# -gt 0 ]]; do
  case $1 in
    --api-key)
      if [ -n "$2" ]; then
        export LLM_API_KEY="$2"
      fi
      shift 2
      ;;
    --mimo-api-key)
      export LLM_API_KEY="$2"
      export MIMO_API_KEY="$2"
      shift 2
      ;;
    --base-url)
      export LLM_BASE_URL="$2"
      shift 2
      ;;
    --model)
      export LLM_MODEL="$2"
      shift 2
      ;;
    --severity)
      SEVERITY="$2"
      shift 2
      ;;
    --max-files)
      export MAX_FILES="$2"
      shift 2
      ;;
    --create-issues)
      CREATE_ISSUES="$2"
      shift 2
      ;;
    --post-comment)
      POST_COMMENT="$2"
      shift 2
      ;;
    --full-scan)
      FULL_SCAN="$2"
      shift 2
      ;;
    --ignore-patterns)
      if [ -n "$2" ]; then
        export IGNORE_PATTERNS="$2"
      fi
      shift 2
      ;;
    --config)
      CONFIG="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

CMD_ARGS=()

if [ -n "$SEVERITY" ]; then
  CMD_ARGS+=("--severity" "$SEVERITY")
fi

if [ "$CREATE_ISSUES" = "true" ]; then
  CMD_ARGS+=("--create-issues")
else
  CMD_ARGS+=("--no-create-issues")
fi

if [ "$POST_COMMENT" = "true" ]; then
  CMD_ARGS+=("--post-comment")
else
  CMD_ARGS+=("--no-post-comment")
fi

if [ "$FULL_SCAN" = "true" ]; then
  CMD_ARGS+=("--full-scan")
else
  CMD_ARGS+=("--diff-only")
fi

if [ -n "$CONFIG" ]; then
  CMD_ARGS+=("--config" "$CONFIG")
fi

CMD_ARGS+=("--output" "/tmp/repo-scanner-report.md")
CMD_ARGS+=("--output-json" "/tmp/repo-scanner-report.json")

python -m repo_scanner.main "${CMD_ARGS[@]}"

echo "report_path=/tmp/repo-scanner-report.md" >> "$GITHUB_OUTPUT"
echo "json_report_path=/tmp/repo-scanner-report.json" >> "$GITHUB_OUTPUT"

ISSUES_COUNT=$(python -c "import json; data=json.load(open('/tmp/repo-scanner-report.json')); print(data.get('total_issues', 0))" 2>/dev/null || echo "0")
echo "total_issues=$ISSUES_COUNT" >> "$GITHUB_OUTPUT"
