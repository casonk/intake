#!/usr/bin/env bash
# apply.sh — create the extractor sandbox in K3s. No sudo, and nothing to prepare on the host: the pod mounts none
# of your files and holds no secret, so there is no folder to label and no credential to resolve. Safe to repeat.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
kubectl wait --for=condition=Ready node --all --timeout=60s
kubectl get crd ciliumnetworkpolicies.cilium.io >/dev/null
# The namespace and the policy exist before the pod does.
kubectl apply -f "$HERE/manifests/00-namespace.yaml"
kubectl apply -f "$HERE/manifests/30-network-policy.yaml"
kubectl apply -f "$HERE/manifests/20-deployment.yaml"
kubectl -n intake-extractor rollout restart deploy/intake-extractor >/dev/null
kubectl -n intake-extractor rollout status deploy/intake-extractor --timeout=300s
echo "Deployed. The daemon is not using it yet: deploy/deploy.sh --mode k3s up verifies it and then switches the daemon over."
