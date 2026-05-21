#!/bin/bash
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  uv venv .venv
fi

if ! .venv/bin/python -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  uv pip install -r requirements.txt
fi

echo "Starting server at http://localhost:8000"
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --reload
