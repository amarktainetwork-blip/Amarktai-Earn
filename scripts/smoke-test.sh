#!/usr/bin/env bash
set -euo pipefail
curl -fsS https://earn.amarktai.co.za/healthz | grep -q '"status": "ok"'
echo "smoke ok"
