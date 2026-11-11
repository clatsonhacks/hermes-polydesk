#!/usr/bin/env bash
# Run a command; on failure, publish the tail of its output as an ::error annotation so the
# result is readable on the public run page without a GitHub login.
set -uo pipefail
title="$1"; shift
out=$(mktemp)
"$@" 2>&1 | tee "$out"
code=${PIPESTATUS[0]}
if [ "$code" -ne 0 ]; then
  body=$(tail -n 80 "$out" | sed -e 's/%/%25/g' | awk '{printf "%s%%0A", $0}')
  echo "::error title=${title} (exit ${code})::${body}"
fi
exit "$code"
