#!/usr/bin/env bash
# Run every toolkit's pytest suite and print a per-toolkit summary.
# Usage: bash run_all_tests.sh [python]
# Default python: the Forex-agent test venv.
set -u
PY="${1:-$HOME/workspace/Forex-agent/.venv/bin/python}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
total_pass=0
total_fail=0
failed_dirs=""
for dir in "$ROOT"/[0-9][0-9]-*/; do
  name="$(basename "$dir")"
  out="$(cd "$dir" && "$PY" -m pytest -q 2>&1 | tail -n 1)"
  echo "$name: $out"
  p="$(echo "$out" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || true)"
  f="$(echo "$out" | grep -oE '[0-9]+ (failed|error)' | grep -oE '^[0-9]+' | head -1 || true)"
  total_pass=$((total_pass + ${p:-0}))
  if [ -n "${f:-}" ] && [ "$f" != "0" ]; then
    total_fail=$((total_fail + f))
    failed_dirs="$failed_dirs $name"
  fi
  if ! echo "$out" | grep -q "passed"; then
    failed_dirs="$failed_dirs $name(no-tests-collected)"
  fi
done
echo "------------------------------------------------------------"
echo "TOTAL: $total_pass passed, $total_fail failed"
if [ -n "$failed_dirs" ]; then
  echo "FAILING/EMPTY SUITES:$failed_dirs"
  exit 1
fi
echo "ALL SUITES GREEN"
