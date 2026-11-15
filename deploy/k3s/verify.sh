#!/usr/bin/env bash
# verify.sh — prove the sandbox is isolated and that it really parses documents. Every check states what it expects;
# one that cannot run is a FAIL, never an "ok".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
NS=intake-extractor
IP=10.43.0.61
PORT="${INTAKE_EXTRACTOR_PORT:-5455}"
fail=0
ok()  { echo "ok    $*"; }
bad() { echo "FAIL  $*"; fail=1; }

# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() { kubectl -n default delete pod intake-extractor-peer --ignore-not-found --wait=false >/dev/null 2>&1; }
trap cleanup EXIT
# A previous run's pod may still be terminating; wait until it is gone before creating the next one.
kubectl -n default delete pod intake-extractor-peer --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true

POD="$(kubectl -n "$NS" get pod -l app=intake-extractor --field-selector=status.phase=Running -o name 2>/dev/null | head -1)"
[ -n "$POD" ] || { echo "FAIL  no running extractor pod; nothing was verified"; exit 2; }

# Use the loopback bridge when it is up (that is the path the daemon takes); otherwise the cluster address.
if systemctl --user is-active --quiet intake-extractor.socket 2>/dev/null; then URL="http://127.0.0.1:$PORT"; WHERE="through the loopback bridge"
else URL="http://$IP:5455"; WHERE="at the cluster address"; fi

# 1. Reachable, and it answers its health check.
if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/healthz")" = 200 ]; then ok "the sandbox answers $WHERE"
else bad "the sandbox did not answer $WHERE"; fi

# 2. It parses real documents, driven through the same client the daemon uses.
result="$(cd "$REPO" && INTAKE_EXTRACTOR=sandbox INTAKE_EXTRACTOR_URL="$URL" INTAKE_EXTRACTOR_TIMEOUT=120 python3 - <<'PY' 2>&1
import io, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from intake import sandbox

try:
    font = ImageFont.load_default(size=64)
except TypeError:
    font = ImageFont.load_default()
image = Image.new("RGB", (900, 220), "white")
ImageDraw.Draw(image).text((30, 60), "TOTAL 12.34", fill="black", font=font)

with tempfile.TemporaryDirectory() as tmp:
    png = Path(tmp) / "receipt.png"
    pdf = Path(tmp) / "receipt.pdf"
    bad = Path(tmp) / "broken.jpg"
    image.save(png)
    image.save(pdf)               # an image-only PDF: pdfplumber finds no text, so tesseract must run
    bad.write_bytes(b"not an image at all")
    out = []
    out.append("png-text=" + str("TOTAL" in sandbox.extract_text(png).upper()))
    out.append("pdf-text=" + str("TOTAL" in sandbox.extract_text(pdf).upper()))
    out.append("png-preview=" + str(sandbox.preview_png(pdf, 100).startswith(b"\x89PNG")))
    out.append("decode-good=" + str(sandbox.image_decodes(png)))
    out.append("decode-bad=" + str(sandbox.image_decodes(bad)))
    print(" ".join(out))
PY
)"
last="$(echo "$result" | tail -1)"
if [ "$last" = "png-text=True pdf-text=True png-preview=True decode-good=True decode-bad=False" ]; then
  ok "it OCRs a PNG and an image-only PDF, renders a PNG preview, and tells a good image from a broken one"
else bad "document parsing did not behave (got: ${last:-nothing})"; fi

# 3. A burst: a page of attachments arrives at once. The bridge and the service must survive it.
if [ "$URL" = "http://127.0.0.1:$PORT" ]; then
  codes="$(seq 1 60 | xargs -P 20 -I{} curl -s -m 10 -o /dev/null -w '%{http_code}\n' "$URL/healthz" | sort | uniq -c | tr -s ' ' | tr '\n' ';')"
  if [ "$codes" = " 60 200;" ]; then ok "the bridge answered a burst of 60 requests (all 200)"; else bad "the bridge failed a burst: $codes"; fi
  if systemctl --user is-active --quiet intake-extractor.socket; then ok "the bridge socket is still listening after the burst"; else bad "the bridge socket died under load"; fi
else echo "skip  the bridge is not on; burst not checked"; fi

# 4. A request it must refuse.
refused="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST --data-binary 'x' "$URL/extract?suffix=.exe")"
if [ "$refused" = 400 ]; then ok "an unsupported file type is refused (400)"; else bad "an unsupported file type was not refused (got $refused)"; fi

# 5. Isolation, from inside the pod.
probe() { # probe <expect: open|closed> <label> <host> <port>
  local expect=$1 label=$2 host=$3 port=$4 got
  got="$(kubectl -n "$NS" exec "$POD" -- python3 -c "
import socket
try:
    socket.create_connection(('$host', $port), timeout=4).close(); print('open')
except OSError:
    print('closed')" 2>/dev/null | tail -1)"
  case "$got" in
    open|closed)
      if [ "$got" = "$expect" ]; then ok "$label ($host:$port is $got)"; else bad "$label ($host:$port is $got, expected $expect)"; fi ;;
    *) bad "$label (could not run the probe inside the pod)" ;;
  esac
}
NODE_IP="$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' | awk '{print $1}')"
probe closed "no internet"                example.com 443
probe closed "no raw IP either"           1.1.1.1 53
probe closed "no Kubernetes API"          kubernetes.default.svc 443
probe closed "no host SSH"                "$NODE_IP" 22
probe closed "no registry"                10.43.0.50 5000
probe closed "no link-local metadata"     169.254.169.254 80

if kubectl -n "$NS" exec "$POD" -- sh -c 'test ! -e /var/run/secrets/kubernetes.io' 2>/dev/null; then ok "no service-account token"; else bad "a service-account token is mounted"; fi
volumes="$(kubectl -n "$NS" get deploy intake-extractor -o jsonpath='{.spec.template.spec.volumes[*].name}' 2>/dev/null)"
if [ "$volumes" = "scratch" ]; then ok "the only volume is the scratch directory (none of your files)"; else bad "unexpected volumes: ${volumes:-unknown}"; fi
envs="$(kubectl -n "$NS" get deploy intake-extractor -o jsonpath='{.spec.template.spec.containers[0].env}{.spec.template.spec.containers[0].envFrom}' 2>/dev/null)"
if [ -z "$envs" ]; then ok "no environment variables or Secrets are passed in"; else bad "the pod is given environment: $envs"; fi

# 6. Another pod cannot reach it, so only the daemon's own machine can send it documents.
kubectl -n default run intake-extractor-peer --restart=Never --image=docker.io/library/busybox:1.37 --command -- \
  sh -c "wget -T4 -qO- http://$IP:5455/healthz && echo REACHED || echo BLOCKED" >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(kubectl -n default get pod intake-extractor-peer -o jsonpath='{.status.phase}' 2>/dev/null)" = Succeeded ] && break; sleep 2; done
peer="$(kubectl -n default logs intake-extractor-peer 2>/dev/null | tail -1)"
case "$peer" in BLOCKED) ok "another pod cannot reach it" ;; REACHED) bad "another pod reached the sandbox" ;; *) bad "peer test did not run (got: ${peer:-nothing})" ;; esac
exit $fail
