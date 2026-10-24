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

STAGE="${SUC_STAGE:-/var/tmp/suc-rehearsal}" # SUC_STAGE exists for tests
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

# A fresh k3s answers the API before its controllers have created everything a manifest names: the
# token controller adds each namespace's `default` ServiceAccount seconds later, and a CRD that was
# just applied is not served until it is Established. The apply or wait then fails with one of the
# errors RACE_RE names. Any other error is real and is never retried.
RACE_RE='not found|NotFound|no matches for kind|unable to recognize|ensure CRDs are installed first'
# SUC_RACE_WAIT_S and SUC_POLL_S exist for tests.
RACE_WAIT_S="${SUC_RACE_WAIT_S:-240}"

# retry_race CMD ARGS...: run the command until it succeeds, retrying only on a RACE_RE error and
# only for RACE_WAIT_S. RACE_INPUT, when set, is the command's stdin on every attempt. The output
# of the last attempt goes to stdout on success and to stderr on failure.
retry_race() {
  local deadline=$((SECONDS + RACE_WAIT_S)) out rc
  while :; do
    # A here-string, not a pipe: a command that never reads stdin would otherwise race the writer,
    # and under pipefail the writer's SIGPIPE fails the attempt.
    out="$("$@" 2>&1 <<<"${RACE_INPUT:-}")"
    rc=$?
    if ((rc == 0)); then
      [[ -z "$out" ]] || printf '%s\n' "$out"
      return 0
    fi
    if ! grep -Eq "$RACE_RE" <<<"$out" || ((SECONDS >= deadline)); then
      printf '%s\n' "$out" >&2
      return "$rc"
    fi
    sleep "${SUC_POLL_S:-2}"
  done
}

# apply_stdin: `kubectl apply -f -` of the manifest on stdin, retried like retry_race. Stdin cannot
# be replayed, so it is read once here.
apply_stdin() {
  local manifest
  manifest="$(cat)"
  RACE_INPUT="$manifest" retry_race "${KUBECTL[@]}" apply -f -
}

# wait_for_default_sa NAMESPACE: the API refuses a Pod until its namespace has a `default` ServiceAccount.
wait_for_default_sa() {
  retry_race "${KUBECTL[@]}" -n "$1" get serviceaccount default -o name >/dev/null \
    || die "serviceaccount/default never appeared in $1 within ${RACE_WAIT_S}s"
}

# wait_for_registry IP: a hostNetwork Pod reads Ready once its container starts, before it listens.
wait_for_registry() {
  local deadline=$((SECONDS + RACE_WAIT_S))
  until curl -fsS --max-time 5 "http://$1:5000/v2/" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || die "the registry at $1:5000 never answered within ${RACE_WAIT_S}s"
    sleep "${SUC_POLL_S:-2}"
  done
}

cmd_k3s() {
  fetch_checked "$2" "$3" /tmp/k3s-install.sh
  # Single node, no add-ons: the Plan and the Flux apply are the subject, not the workloads.
  INSTALL_K3S_VERSION="$1" sh /tmp/k3s-install.sh --disable traefik --disable servicelb \
    --disable metrics-server --disable local-storage || die "k3s $1 did not install"
  wait_for_node_ready
}

# `kubectl wait --all` exits at once with "no matching resources found" while no Node object
# exists, and the installer returns before the kubelet registers one. Poll for the Node first,
# then spend the rest of the one budget on Ready. SUC_NODE_WAIT_S and SUC_POLL_S exist for tests.
wait_for_node_ready() {
  local budget="${SUC_NODE_WAIT_S:-240}" deadline left
  deadline=$((SECONDS + budget))
  until [[ -n "$("${KUBECTL[@]}" get nodes -o name 2>/dev/null)" ]]; do
    ((SECONDS < deadline)) || die "no node registered within ${budget}s"
    sleep "${SUC_POLL_S:-2}"
  done
  left=$((deadline - SECONDS))
  ((left > 0)) || left=1
  "${KUBECTL[@]}" wait --for=condition=Ready node --all --timeout="${left}s" || die "the node never read Ready"
}

cmd_flux_cli() {
  fetch_checked "$1" "$2" /tmp/flux.tar.gz
  tar -xzf /tmp/flux.tar.gz -C /usr/local/bin flux || die "the flux tarball holds no flux binary"
}

cmd_base() {
  wait_for_default_sa kube-system
  # hostNetwork, so source-controller reaches it at the node address with no CNI dependence.
  apply_stdin <<EOF || die "the registry pod was not applied"
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
    '  name: homelab-critical' 'value: 100000' | apply_stdin || die "no PriorityClass"
  retry_race "${KUBECTL[@]}" apply --server-side -f "$STAGE/hand/components.yaml" \
    || die "Flux's components were not applied"
  # `apply` returns when a CRD is stored, not when it is served. The OCIRepository and the
  # Kustomization that `publish` applies need both of these Established.
  retry_race "${KUBECTL[@]}" wait --for=condition=Established --timeout=60s \
    crd/ocirepositories.source.toolkit.fluxcd.io crd/kustomizations.kustomize.toolkit.fluxcd.io \
    || die "Flux's CRDs never became Established"
  local d
  for d in source-controller kustomize-controller notification-controller; do
    retry_race "${KUBECTL[@]}" -n flux-system rollout status "deployment/$d" --timeout=240s \
      || die "$d never rolled out"
  done
}

cmd_publish() {
  local out digest
  wait_for_registry "$1"
  out="$(flux push artifact "oci://$1:5000/homelab:rehearsal" --path="$STAGE/bundle" \
    --source=suc-rehearsal --revision="rehearsal@sha1:$(printf '%040d' 0)" \
    --insecure-registry --output=json)" || die "flux push artifact failed"
  digest="$(printf '%s' "$out" | sed -n 's/.*"digest": *"\(sha256:[0-9a-f]*\)".*/\1/p' | head -1)"
  [[ -n "$digest" ]] || die "flux push printed no digest: $out"
  # The guest's OCIRepository is named like production's, so the rendered Kustomization follows it.
  apply_stdin <<EOF || die "the OCIRepository was not applied"
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata: {name: homelab, namespace: flux-system}
spec: {interval: 1m, url: "oci://$1:5000/homelab", insecure: true, ref: {tag: rehearsal}}
EOF
  retry_race "${KUBECTL[@]}" apply -f "$STAGE/hand/kustomization.yaml" || die "the Kustomization was not applied"
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
