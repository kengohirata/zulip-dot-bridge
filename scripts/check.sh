#!/usr/bin/env bash
# Offline checks only: no credentials, service calls, deployment, or dependency install.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
(
  cd "$root/backend"
  python3 -m unittest discover -s tests -v
  python3 -m unittest aws.test_checkpoint aws.test_state -v
  python3 - <<'PY'
import ast
from pathlib import Path
files=[Path('bridge.py'), Path('portable.py'), *Path('aws').glob('*.py'), *Path('tests').glob('*.py')]
for file in files:
    ast.parse(file.read_text(), filename=str(file))
print(f'PASS: Python syntax ({len(files)} files)')
PY
)
(
  cd "$root/site"
  node --check worker/index.js
  node scripts/test-probe.mjs
  node scripts/test-bridge.mjs
)
# Build a copy with a placeholder configuration. Never touch a deployed checkout.
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
mkdir -p "$temp/site/.openai"
cp -R "$root/site/worker" "$root/site/scripts" "$root/site/drizzle" "$temp/site/"
cp "$root/site/package.json" "$temp/site/"
cp "$root/site/.openai/hosting.example.json" "$temp/site/.openai/hosting.json"
(
  cd "$temp/site"
  bash scripts/build.sh
  node scripts/validate-artifact.mjs
)
echo 'PASS: all offline checks; no deployment or live integration was tested'
