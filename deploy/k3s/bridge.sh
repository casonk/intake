#!/usr/bin/env bash
# bridge.sh on|off|status — put the sandbox pod behind 127.0.0.1:5455 (or take it away). No sudo.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS="${INTAKE_UNITS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
PORT="${INTAKE_EXTRACTOR_PORT:-5455}"
case "${1:-status}" in
  on)
    install -d "$UNITS"
    install -m 644 "$HERE/systemd/intake-extractor.socket" "$HERE/systemd/intake-extractor.service" "$UNITS/"
    systemctl --user daemon-reload
    systemctl --user enable --now intake-extractor.socket
    for _ in $(seq 1 15); do
      if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then echo "sandbox answering on 127.0.0.1:$PORT"; exit 0; fi
      sleep 2
    done
    echo "the sandbox did not answer on 127.0.0.1:$PORT" >&2
    exit 1
    ;;
  off) systemctl --user disable --now intake-extractor.socket intake-extractor.service 2>/dev/null || true ;;
  status) printf '%-26s %s\n' intake-extractor.socket "$(systemctl --user is-active intake-extractor.socket 2>/dev/null || true)" ;;
  *) echo "usage: $0 on|off|status"; exit 2 ;;
esac
