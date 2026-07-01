#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x "venv/bin/python" ]; then
  python3 -m venv venv
fi

# shellcheck source=/dev/null
source venv/bin/activate
if ! python -c "import streamlit" >/dev/null 2>&1; then
  rm -f "venv/.deps_installed"
fi

if [ ! -f "venv/.deps_installed" ]; then
  echo "Installing Python dependencies. This may take a few minutes on first launch..."
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  printf 'ok\n' > "venv/.deps_installed"
fi

python -m streamlit run app.py --server.port 8501
