#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_NAME="${1:-yk-ferta-dev-cpu}"

micromamba create -y -f "$ROOT_DIR/envs/yk-ferta-dev-cpu.yaml"

PYTHONNOUSERSITE=1 micromamba run -n "$ENV_NAME" python -m pip install -e "$ROOT_DIR"
