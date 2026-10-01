#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
if [[ ! -x .venv/bin/python ]]; then
  uv venv .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python manage.py migrate
if [[ "${1:-web}" == "worker" ]]; then
  exec .venv/bin/python manage.py dispatch_alerts
fi
exec .venv/bin/python manage.py runserver 127.0.0.1:8020
