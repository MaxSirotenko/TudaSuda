#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x "venv/bin/python" ]; then
  python3 -m venv venv
fi

# shellcheck source=/dev/null
source venv/bin/activate
REQ_HASH="$(python -c 'from pathlib import Path; import hashlib; p=Path("requirements.txt"); print(hashlib.sha256(p.read_bytes()).hexdigest())')"
REQ_HASH_FILE="venv/.requirements.sha256"
INSTALLED_REQ_HASH=""
if [ -f "$REQ_HASH_FILE" ]; then
  INSTALLED_REQ_HASH="$(cat "$REQ_HASH_FILE")"
fi

if [ "$REQ_HASH" != "$INSTALLED_REQ_HASH" ]; then
  echo "Installing Python dependencies. This may take a few minutes..."
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  printf '%s\n' "$REQ_HASH" > "$REQ_HASH_FILE"
  printf 'ok\n' > "venv/.deps_installed"
fi

python -m streamlit run virtual_warehouse_app.py --server.address 127.0.0.1 --server.port 8501
