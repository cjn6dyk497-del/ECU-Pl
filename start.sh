#!/bin/bash
cd "$(dirname "$0")"
python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
