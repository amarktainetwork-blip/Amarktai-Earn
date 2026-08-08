#!/bin/bash
set -euo pipefail
cd /workspace
agent="${AMARKTAI_AGENT:-}"
case "$agent" in
  aider)
    exec aider \
      --model "${AIDER_MODEL:?}" \
      --openai-api-base "${AIDER_OPENAI_API_BASE:?}" \
      --openai-api-key "${AIDER_OPENAI_API_KEY:?}" \
      --no-stream \
      --yes-always \
      --no-auto-commits \
      --no-check-update \
      --analytics-disable \
      --disable-playwright \
      --config /dev/null \
      --env-file /dev/null \
      --message "${AMARKTAI_TASK:?}"
    ;;
  openhands)
    export RUNTIME=process
    export OH_PERSISTENCE_DIR=/tmp/openhands
    exec openhands --headless --json --override-with-envs -t "${AMARKTAI_TASK:?}"
    ;;
  ci)
    exec /bin/bash -lc "${AMARKTAI_TEST_COMMAND:?}"
    ;;
  *)
    echo "unsupported AMARKTAI_AGENT" >&2
    exit 64
    ;;
esac
