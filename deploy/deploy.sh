#!/usr/bin/env bash
# deploy.sh — choose where intake parses untrusted receipt files: dev, host or k3s. One entry point, one setting.
#
#   deploy/deploy.sh [--mode dev|host|k3s] up|down|status|verify|mode
#
# The mode comes from, first match wins:
#   1. --mode on the command line
#   2. INTAKE_DEPLOY_MODE in the environment
#   3. INTAKE_DEPLOY_MODE in config/deploy.local.env (copy config/deploy.example.env; never committed)
#   4. dev
#
# The intake daemon itself always runs on the host (Clockwork manages it). What changes is where its PDF, image
# and video parsers run: in the daemon's process (host), in the sandbox service on this machine (dev), or in the
# sandbox service in a K3s pod with no network and no secrets (k3s). Only k3s mode touches kubectl or podman.
# See docs/deployment.md.
# -E: the ERR trap used by `k3s up` must also fire for failures inside functions (bridge, install_dropin).
set -eEuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
K3S_DIR="${INTAKE_K3S_DIR:-$HERE/k3s}"
CONFIG_FILE="${INTAKE_DEPLOY_CONFIG:-$REPO/config/deploy.local.env}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/intake"
MARKER="$STATE_DIR/deploy-mode"
UNITS_DIR="${INTAKE_UNITS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
DAEMON="intake-daemon.service"
DROPIN_DIR="$UNITS_DIR/$DAEMON.d"
DROPIN="$DROPIN_DIR/20-extractor-sandbox.conf"
PORT="${INTAKE_EXTRACTOR_PORT:-5455}"
MODES="dev host k3s"

usage() {
  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

mode="" source_of=""
cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) mode="${2:?--mode needs dev, host or k3s}"; source_of="--mode"; shift 2 ;;
    --mode=*) mode="${1#--mode=}"; source_of="--mode"; shift ;;
    -h|--help) usage 0 ;;
    up|down|status|verify|mode) cmd="$1"; shift ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ -n "$cmd" ] || usage 2

if [ -z "$mode" ] && [ -n "${INTAKE_DEPLOY_MODE:-}" ]; then mode="$INTAKE_DEPLOY_MODE"; source_of="environment"; fi
if [ -z "$mode" ] && [ -f "$CONFIG_FILE" ]; then
  # No `|| true`: if reading the config fails, say so rather than quietly choosing dev.
  line="$(python3 -I "$K3S_DIR/env_value.py" "$CONFIG_FILE" INTAKE_DEPLOY_MODE)"
  if [ -n "$line" ]; then mode="${line#INTAKE_DEPLOY_MODE=}"; source_of="$CONFIG_FILE"; fi
fi
if [ -z "$mode" ]; then mode="dev"; source_of="default"; fi
case " $MODES " in *" $mode "*) ;; *) echo "unknown deployment mode '$mode' (from $source_of); use one of: $MODES" >&2; exit 2 ;; esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "$2" >&2; exit 1; }; }
marker_is_k3s() { [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = k3s ]; }
set_marker() { mkdir -p "$STATE_DIR"; echo "$1" > "$MARKER"; }
clear_marker() { rm -f "$MARKER"; }
healthz() { curl -fsS --max-time 5 "http://127.0.0.1:$PORT/healthz" && echo; }

# The daemon uses the sandbox only while this drop-in exists. Writing it and removing it are the whole switch.
install_dropin() {
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN" <<E
# Managed by intake's deploy/deploy.sh. Remove with: deploy/deploy.sh --mode host up
[Service]
Environment=INTAKE_EXTRACTOR=sandbox
Environment=INTAKE_EXTRACTOR_URL=http://127.0.0.1:$PORT
E
  systemctl --user daemon-reload
  systemctl --user restart "$DAEMON"
}
remove_dropin() {
  rm -f "$DROPIN"
  systemctl --user daemon-reload
  systemctl --user restart "$DAEMON"
}
bridge() { "$K3S_DIR/bridge.sh" "$1"; }

case "$cmd:$mode" in
  mode:*) echo "$mode (from $source_of)" ;;

  # ---- dev: the sandbox service in the foreground on this machine, no container ----
  up:dev)
    echo "extractor sandbox on 127.0.0.1:$PORT. To use it: INTAKE_EXTRACTOR=sandbox INTAKE_EXTRACTOR_URL=http://127.0.0.1:$PORT" >&2
    exec env PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" python3 -m intake.extractor_service --host 127.0.0.1 --port "$PORT"
    ;;
  down:dev) echo "dev mode runs the sandbox service in the foreground; stop it with Ctrl-C" ;;
  status:dev|verify:dev) healthz || echo "no sandbox answering on 127.0.0.1:$PORT (deploy/deploy.sh up starts one)" ;;

  # ---- host: the daemon parses in its own process, as intake always has ----
  up:host)
    if marker_is_k3s; then
      echo "the K3s sandbox is active; switching the daemon back to in-process parsing"
      bridge off || true
      if command -v "${KUBECTL:-kubectl}" >/dev/null 2>&1; then
        KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}" "${KUBECTL:-kubectl}" -n intake-extractor scale deploy/intake-extractor --replicas=0 || true
      else
        echo "kubectl is not installed: the pod keeps running, idle; stop it with: kubectl -n intake-extractor scale deploy/intake-extractor --replicas=0" >&2
      fi
    fi
    remove_dropin
    clear_marker
    ;;
  down:host) echo "host mode has no service of its own: the daemon is managed by Clockwork (systemctl --user stop $DAEMON)" ;;
  status:host)
    printf '%-26s %-9s sandbox drop-in: %s\n' "$DAEMON" "$(systemctl --user is-active "$DAEMON" 2>/dev/null || true)" \
      "$([ -f "$DROPIN" ] && echo present || echo absent)" ;;
  verify:host) [ ! -f "$DROPIN" ] && echo "the daemon parses in-process (no sandbox drop-in)" ;;

  # ---- k3s: the sandbox in a pod with no network, no secrets and no files ----
  up:k3s)
    need "${KUBECTL:-kubectl}" "k3s mode needs kubectl and a running K3s (see docs/deployment.md). dev and host modes need neither."
    need podman "k3s mode needs podman, to build the image"
    export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
    curl -fsS --max-time 5 http://10.43.0.50:5000/v2/ >/dev/null 2>&1 || {
      echo "k3s mode needs the in-cluster registry: run install-registry.sh and enable-registry-mirror.sh from crew-chief's deploy/k3s" >&2
      exit 1
    }
    "$K3S_DIR/build-image.sh"
    "$K3S_DIR/apply.sh"
    # Anything that fails from here on leaves the daemon parsing in-process, as it was: the bridge goes back
    # down, and the daemon is restarted only if it had already been pointed at the sandbox.
    restore() {
      echo "the K3s sandbox could not be brought up; keeping in-process parsing" >&2
      if [ -f "$DROPIN" ]; then
        rm -f "$DROPIN"
        systemctl --user daemon-reload || true
        systemctl --user restart "$DAEMON" || true
      fi
      bridge off || true
    }
    trap restore ERR
    # The bridge goes up and is verified before the daemon is pointed at it, so the daemon never sees a dead port.
    bridge on
    "$K3S_DIR/verify.sh"
    install_dropin
    trap - ERR
    set_marker k3s
    ;;
  down:k3s) "$0" --mode host up; echo "rolled back: the daemon parses in-process again; the pod is scaled to 0 and left in place (kubectl delete ns intake-extractor removes it)" ;;
  status:k3s)
    printf '%-26s %-9s sandbox drop-in: %s\n' "$DAEMON" "$(systemctl --user is-active "$DAEMON" 2>/dev/null || true)" \
      "$([ -f "$DROPIN" ] && echo present || echo absent)"
    bridge status || true
    KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}" "${KUBECTL:-kubectl}" -n intake-extractor get deploy,pods 2>&1 || true ;;
  verify:k3s) "$K3S_DIR/verify.sh" ;;
esac
