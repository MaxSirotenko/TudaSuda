#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x "venv/bin/python" ]; then
  python3 -m venv venv
fi

# shellcheck source=/dev/null
source venv/bin/activate
if [ ! -f "venv/.deps_installed" ]; then
  echo "Installing Python dependencies. This may take a few minutes on first launch..."
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  printf 'ok\n' > "venv/.deps_installed"
fi

python -m streamlit run row_constructor.py --server.address 127.0.0.1 --server.port 8502
