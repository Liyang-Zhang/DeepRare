#!/usr/bin/env bash
set -euo pipefail

export PYTHONUNBUFFERED=1
export HF_HOME="${HF_HOME:-/app/.cache/huggingface}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-$HF_HOME/transformers}"
export YK_FERTA_CONFIG_PATH="${YK_FERTA_CONFIG_PATH:-/app/config/clinical_mvp.json}"
export YK_FERTA_DB_PATH="${YK_FERTA_DB_PATH:-/app/runtime/yk_ferta.sqlite3}"
export YK_FERTA_HOST="${YK_FERTA_HOST:-0.0.0.0}"
export YK_FERTA_PORT="${YK_FERTA_PORT:-8000}"

mkdir -p "$(dirname "$YK_FERTA_DB_PATH")" "$HF_HOME" "$TRANSFORMERS_CACHE"

exec uvicorn yk_ferta.api.app:app --host "$YK_FERTA_HOST" --port "$YK_FERTA_PORT"
