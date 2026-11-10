#!/usr/bin/env bash
# Start Stampd locally at http://localhost:8000
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
[ -d samples/pdfs ] || python3 samples/generate.py
python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
