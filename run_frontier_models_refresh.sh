#!/bin/bash
# run_frontier_models_refresh.sh — weekly local refresh of augmentyourexperience's
# data/frontier-models.json. Runs via cron (Saturday mornings) because vendor doc
# domains (OpenAI, Google, xAI, Mistral, Meta, DeepSeek, Alibaba) are blocked by
# the Claude Code cloud sandbox's network egress policy but are reachable fine
# from this machine. Uses the local `claude` CLI headlessly with the same prompt
# that was originally set up as a cloud routine.

set -e

AYX_DIR="/Users/jennlee/Projects/augmentyourexperience-www"
PROMPT_FILE="/Users/jennlee/Projects/morning-brief/frontier_models_prompt.txt"
CLAUDE_BIN="/Users/jennlee/.local/bin/claude"

echo ""
echo "────────────────────────────────────────────────────"
echo "▶  Frontier models refresh: $(date '+%Y-%m-%d %H:%M:%S')"

cd "$AYX_DIR"
git checkout main -q
git pull --ff-only -q

"$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
  --allowedTools "Bash,Read,Write,Edit,Glob,Grep,WebFetch" \
  --permission-mode bypassPermissions \
  --max-budget-usd 2.00 \
  --output-format json
