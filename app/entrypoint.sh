#!/usr/bin/env bash
# Entrypoint for the nilan-api container.
#
# Real mode (NILAN_MOCKUP=0): start the ordered tunnel supervisor to bridge the
#   primary and optional fallback raw-TCP endpoint to one in-container PTY.
#   It tries one owner at a time with bounded connects and retry backoff. The
#   process supervisor restarts the container if either tunnel or API child dies.
# Mockup mode (NILAN_MOCKUP=1): no socat; the wrapper uses CTS600Mockup.
set -euo pipefail

exec python /app/process_supervisor.py
