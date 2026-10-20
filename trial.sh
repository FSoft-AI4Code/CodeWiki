#!/usr/bin/env bash
set -euo pipefail

# Four-repo Gemini trial for CodeWiki
# Run from any directory. Clones repos into a temp dir, generates docs,
# and prints a cost/timing summary.
#
# Prerequisites:
#   - codewiki installed from the fork branch with google-vertex configured
#   - ADC login done (gcloud auth application-default login)
#   - gh cli authenticated for ELIX-Group

REPOS=(
  "ELIX-Group/incident-triage-agent"
  "ELIX-Group/ELIX_Weather_Data"
  "ELIX-Group/ELIX-Twikey-Sync"
  "ELIX-Group/ELIX-Sync-Envitron-Stateofcharge"
)

OPUS_COSTS=(4.51 22.75 0.32 0.32)

# Pass the work dir as $1, e.g.: bash trial.sh ~/code/CodeWiki/examples
WORK_DIR="${1:?Usage: $0 <work-dir, e.g. ~/code/CodeWiki/examples>}"
SUMMARY_FILE="$WORK_DIR/trial-summary.txt"

echo "=== CodeWiki Gemini Trial ==="
echo "Work dir: $WORK_DIR"
echo "Started:  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

# Verify config
echo "--- Config ---"
codewiki config show --json 2>/dev/null | python3 -m json.tool | grep -E '"provider"|"main_model"|"google_location"|"google_project"'
echo ""

mkdir -p "$WORK_DIR"

{
  printf "%-40s %8s %10s %12s %10s\n" "Repo" "Time(s)" "Tokens" "Cost(USD)" "Opus(USD)"
  printf "%-40s %8s %10s %12s %10s\n" "----" "-------" "------" "---------" "---------"
} > "$SUMMARY_FILE"

total_cost=0
total_opus=0
total_time=0
i=0

for repo in "${REPOS[@]}"; do
  name="${repo#*/}"
  repo_dir="$WORK_DIR/$name"
  opus_cost="${OPUS_COSTS[$i]}"

  echo "=== [$((i+1))/4] $name ==="

  # Clone if not already there
  if [ ! -d "$repo_dir" ]; then
    echo "  Cloning..."
    gh repo clone "$repo" "$repo_dir" -- --depth=1 -q 2>&1 || {
      echo "  FAILED to clone $repo -- skipping"
      i=$((i+1))
      continue
    }
  fi

  # Clean any existing docs
  rm -rf "$repo_dir/docs"

  # Generate
  echo "  Generating docs..."
  start_ts=$(date +%s)

  if codewiki generate --no-prompt-caching -o "$repo_dir/docs" -r "$repo_dir" 2>&1 | tee "$WORK_DIR/${name}.log"; then
    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))

    # Extract stats from metadata.json
    meta="$repo_dir/docs/metadata.json"
    if [ -f "$meta" ]; then
      tokens=$(python3 -c "
import json, sys
m = json.load(open('$meta'))
u = m.get('usage', {}).get('totals', {})
print(int(u.get('input_tokens', 0) + u.get('output_tokens', 0) + u.get('cache_read_tokens', 0)))
" 2>/dev/null || echo "0")

      cost=$(python3 -c "
import json
m = json.load(open('$meta'))
c = m.get('usage', {}).get('totals', {}).get('cost_usd', 0)
print(f'{c:.4f}' if c else '0.0000')
" 2>/dev/null || echo "0.0000")
    else
      tokens="0"
      cost="0.0000"
    fi

    # Count generated files
    n_files=$(find "$repo_dir/docs" -name '*.md' | wc -l | tr -d ' ')

    echo "  Done: ${elapsed}s, ${n_files} files, ${tokens} tokens, \$${cost}"
    printf "%-40s %8d %10s %12s %10s\n" "$name" "$elapsed" "$tokens" "\$$cost" "\$$opus_cost" >> "$SUMMARY_FILE"

    total_cost=$(python3 -c "print(f'{$total_cost + $cost:.4f}')")
    total_time=$((total_time + elapsed))
  else
    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))
    echo "  FAILED after ${elapsed}s (see $WORK_DIR/${name}.log)"
    printf "%-40s %8s %10s %12s %10s\n" "$name" "FAIL" "-" "-" "\$$opus_cost" >> "$SUMMARY_FILE"
  fi

  total_opus=$(python3 -c "print(f'{$total_opus + $opus_cost:.2f}')")
  i=$((i+1))
  echo ""
done

# Totals
{
  printf "%-40s %8s %10s %12s %10s\n" "----" "-------" "------" "---------" "---------"
  printf "%-40s %8d %10s %12s %10s\n" "TOTAL" "$total_time" "-" "\$$total_cost" "\$$total_opus"
} >> "$SUMMARY_FILE"

echo ""
echo "=== Trial Summary ==="
cat "$SUMMARY_FILE"
echo ""
echo "Savings: Opus \$$total_opus -> Gemini Flash \$$total_cost"
echo ""
echo "Docs are in $WORK_DIR/<repo>/docs/"
echo "Logs are in $WORK_DIR/<repo>.log"
echo "Compare with existing Opus output at dev-docs.elix.nl/repos/"
