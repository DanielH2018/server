#!/usr/bin/env bash
# suc-rehearsal-guest-run.sh — the in-guest half of the drill orchestrator's SUC rehearsal mode
# (H6.1b2; docs/target-architecture/host/h6-k3s-suc.md, "Rehearsal in a drill guest"). Copied into
# the throwaway guest by etcd-restore-drill-vm --rehearse-suc, which calls one subcommand per ssh
# session so that no session stays open for the whole run.
#
# Usage: suc-rehearsal-guest-run SUBCOMMAND [ARGS]
#   k3s VERSION INSTALLER_URL SHA256   install k3s VERSION from the installer, checked against SHA256
#   flux-cli URL SHA256                install the flux CLI from a release tarball, checked likewise
#   base REGISTRY_IMAGE                start a registry on the node, then Flux's controllers
#   publish GUEST_IP                   push /var/tmp/suc-rehearsal/bundle, then apply what follows it
#   snapshot HOSTNAME PLAN             print one JSON object of the objects the verdict reads
#   state                              print the objects and the k3s log for the evidence directory
# Root only. It holds no production credential: the orchestrator stages none into this guest.
set -uo pipefail

STAGE=/var/tmp/suc-rehearsal
KUBECTL=(k3s kubectl)

die() { echo "suc-rehearsal-guest-run: $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "must run as root"

# fetch_checked URL SHA256 DEST: a download that does not match its pin is never used.
fetch_checked() {
  curl -fsSL --max-time 180 -o "$3" "$1" || die "download of $1 failed"
  echo "$2  $3" | sha256sum -c --strict - >/dev/null || die "$1 does not match its pinned sha256"
}

cmd_k3s() {
  fetch_checked "$2" "$3" /tmp/k3s-install.sh
  # Single node, no add-ons: the Plan and the Flux apply are the subject, not the workloads.
  INSTALL_K3S_VERSION="$1" sh /tmp/k3s-install.sh --disable traefik --disable servicelb \
    --disable metrics-server --disable local-storage || die "k3s $1 did not install"
  "${KUBECTL[@]}" wait --for=condition=Ready node --all --timeout=240s || die "the node never read Ready"
}

cmd_flux_cli() {
  fetch_checked "$1" "$2" /tmp/flux.tar.gz
  tar -xzf /tmp/flux.tar.gz -C /usr/local/bin flux || die "the flux tarball holds no flux binary"
}

cmd_base() {
  # hostNetwork, so source-controller reaches it at the node address with no CNI dependence.
  "${KUBECTL[@]}" apply -f - <<EOF || die "the registry pod was not applied"
apiVersion: v1
kind: Pod
metadata: {name: registry, namespace: kube-system}
spec:
  hostNetwork: true
  containers:
    - {name: registry, image: "$1", env: [{name: REGISTRY_HTTP_ADDR, value: "0.0.0.0:5000"}]}
EOF
  "${KUBECTL[@]}" -n kube-system wait --for=condition=Ready pod/registry --timeout=240s \
    || die "the registry never read Ready"
  # The Flux pods name this class (H1's steps 1 to 3 install it on a real cluster).
  printf '%s\n' 'apiVersion: scheduling.k8s.io/v1' 'kind: PriorityClass' 'metadata:' \
    '  name: homelab-critical' 'value: 100000' | "${KUBECTL[@]}" apply -f - || die "no PriorityClass"
  "${KUBECTL[@]}" apply --server-side -f "$STAGE/hand/components.yaml" || die "Flux's components were not applied"
  local d
  for d in source-controller kustomize-controller notification-controller; do
    "${KUBECTL[@]}" -n flux-system rollout status "deployment/$d" --timeout=240s || die "$d never rolled out"
  done
}

cmd_publish() {
  local out digest
  out="$(flux push artifact "oci://$1:5000/homelab:rehearsal" --path="$STAGE/bundle" \
    --source=suc-rehearsal --revision="rehearsal@sha1:$(printf '%040d' 0)" \
    --insecure-registry --output=json)" || die "flux push artifact failed"
  digest="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])')" \
    || die "flux push printed no digest"
  # The guest's OCIRepository is named like production's, so the rendered Kustomization follows it.
  "${KUBECTL[@]}" apply -f - <<EOF || die "the OCIRepository was not applied"
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata: {name: homelab, namespace: flux-system}
spec: {interval: 1m, url: "oci://$1:5000/homelab", insecure: true, ref: {tag: rehearsal}}
EOF
  "${KUBECTL[@]}" apply -f "$STAGE/hand/kustomization.yaml" || die "the Kustomization was not applied"
  echo "digest=$digest"
}

# One object, or the JSON `null`: a missing object is an answer, and so is an API server that is
# restarting under the Plan's own upgrade.
get_json() { "${KUBECTL[@]}" get "$@" -o json 2>/dev/null || echo null; }

cmd_snapshot() {
  printf '{"kustomization": %s, "plan": %s, "node": %s}\n' \
    "$(get_json -n flux-system kustomization system-upgrade)" \
    "$(get_json -n system-upgrade plans.upgrade.cattle.io "$2")" \
    "$(get_json node "$1")"
}

cmd_state() {
  "${KUBECTL[@]}" get nodes -o wide
  "${KUBECTL[@]}" get ocirepository,kustomization -A -o yaml
  "${KUBECTL[@]}" -n system-upgrade get plans.upgrade.cattle.io,jobs,pods -o yaml
  journalctl -u k3s --no-pager -n 200
}

sub="${1:-}"
[[ -n "$sub" ]] && shift
case "$sub" in
  k3s) [[ $# -eq 3 ]] || die "usage: k3s VERSION INSTALLER_URL SHA256"; cmd_k3s "$@" ;;
  flux-cli) [[ $# -eq 2 ]] || die "usage: flux-cli URL SHA256"; cmd_flux_cli "$@" ;;
  base) [[ $# -eq 1 ]] || die "usage: base REGISTRY_IMAGE"; cmd_base "$@" ;;
  publish) [[ $# -eq 1 ]] || die "usage: publish GUEST_IP"; cmd_publish "$@" ;;
  snapshot) [[ $# -eq 2 ]] || die "usage: snapshot HOSTNAME PLAN"; cmd_snapshot "$@" ;;
  state) cmd_state ;;
  *) die "unknown subcommand '$sub'" ;;
esac
