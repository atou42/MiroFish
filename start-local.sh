#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "Missing .env. Copy .env.example to .env and fill the required keys."
  exit 1
fi

missing_keys=()
for key in LLM_API_KEY ZEP_API_KEY; do
  value="$(grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- || true)"
  if [[ -z "${value}" ]] || [[ "${value}" == your_* ]]; then
    missing_keys+=("$key")
  fi
done

if (( ${#missing_keys[@]} > 0 )); then
  echo "Fill these keys in $ROOT_DIR/.env before starting:"
  printf '  - %s\n' "${missing_keys[@]}"
  exit 1
fi

echo "Starting MiroFish..."
echo "Frontend: http://localhost:3000"
echo "Backend:  http://localhost:5001"

npm run dev
