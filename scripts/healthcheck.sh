#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"

echo "Checking ${BASE_URL}/app/health"
curl -s "${BASE_URL}/app/health"
echo
echo
echo "Checking ${BASE_URL}/time"
curl -s "${BASE_URL}/time"
echo

