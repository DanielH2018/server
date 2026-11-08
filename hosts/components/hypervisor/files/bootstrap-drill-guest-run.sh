#!/usr/bin/env bash
# bootstrap-drill-guest-run.sh — the in-guest half of the drill orchestrator's bootstrap drill
# (H7.15c's host half; docs/target-architecture/host/h7-delete-ansible.md, *The proofs*). Copied
# into the throwaway guest by etcd-restore-drill-vm --rehearse-bootstrap, which calls one
# subcommand per ssh session and runs the long ones detached.
#
# Usage: bootstrap-drill-guest-run SUBCOMMAND [ARGS]
#   detached STEP [ARGS]        run STEP in the background and return; see `rc` and `out`
#   rc STEP / out STEP          print a detached STEP's exit code (nothing while it runs) or output
#   prepare SHA                 name the guest daniel-box, install git, clone the repository at SHA
#   until4                      bootstrap-host.sh daniel-box --until 4 --local-build <clone>
#   deviate GUEST_IP SSH_FROM   the three guest-local deviations; the masks come from $STAGE/masks
#   bootstrap                   bootstrap-host.sh daniel-box --local-build <clone>
#   start                       systemctl enable --now k3s.service
#   check                       exit 0 when the host-half check passes, printing what it read
#   state                       print the units, ufw and the k3s log for the evidence directory
# Root only. It holds no production credential: the orchestrator stages none into this guest.
set -uo pipefail

STAGE="${BOOTSTRAP_DRILL_STAGE:-/var/tmp/bootstrap-drill}" # BOOTSTRAP_DRILL_STAGE exists for tests
ROOT="${BOOTSTRAP_DRILL_ROOT:-}"                           # so does BOOTSTRAP_DRILL_ROOT
CLONE="$STAGE/clone"
RUNDIR="$STAGE/run"
REPO_URL=https://github.com/DanielH2018/server.git

die() { echo "bootstrap-drill-guest-run: $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "must run as root"

# The orchestrator polls STAGE/STEP.rc over fresh ssh sessions, so no session is held open for a
# step that builds a venv, installs three packages and writes the AIDE baseline.
cmd_detached() {
  local step="$1"
  shift
  rm -f "$STAGE/$step.rc" "$STAGE/$step.out"
  # shellcheck disable=SC2016 # the inner shell expands these
  OUT="$STAGE/$step.out" RC="$STAGE/$step.rc" nohup bash -c '"$@" >"$OUT" 2>&1; echo $? >"$RC"' \
    _ bash "$0" "$step" "$@" >/dev/null 2>&1 </dev/null &
  echo "started $step (pid $!)"
}

cmd_prepare() {
  # Step 1 of bootstrap-host.sh refuses any hostname but the one it is given; the seed names the
  # guest etcd-drill.
  hostnamectl set-hostname daniel-box || die "cannot set the hostname"
  grep -q '[[:space:]]daniel-box$' "$ROOT/etc/hosts" || echo "127.0.1.1 daniel-box" >>"$ROOT/etc/hosts"
  # A first boot's cloud-init and apt-daily jobs hold dpkg's lock, and step 5's install does not
  # wait for it. The stop waits for a running job to end.
  cloud-init status --wait >/dev/null 2>&1 || true
  systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service \
    || die "cannot stop the apt-daily jobs"
  apt-get update || die "apt-get update failed"
  apt-get install -y git gnupg curl || die "apt-get install git gnupg curl failed"
  rm -rf "$CLONE"
  git clone --quiet "$REPO_URL" "$CLONE" || die "git clone of $REPO_URL failed"
  git -C "$CLONE" checkout --quiet --detach "$1" || die "$1 is not in $REPO_URL"
  install -d "$RUNDIR"
  cp "$CLONE/hosts/bootstrap-host.sh" "$CLONE/hosts/keys/homelab-archive-keyring.gpg" "$RUNDIR/" \
    || die "the clone holds no bootstrap-host.sh or keyring"
}

bootstrap() {
  cd "$RUNDIR" || die "$RUNDIR is missing: run prepare first"
  ./bootstrap-host.sh daniel-box "$@" --local-build "$CLONE"
}

cmd_deviate() {
  local unit
  # The packaged 50-homelab.yaml pins both to daniel-box's LAN address, which this guest must
  # never hold. etcd-s3 is off because Flux, which applies its credential Secret, never runs here.
  install -d "$ROOT/etc/rancher/k3s/config.yaml.d"
  printf 'node-ip: "%s"\nadvertise-address: "%s"\netcd-s3: false\n' "$1" "$1" \
    >"$ROOT/etc/rancher/k3s/config.yaml.d/90-drill.yaml" || die "cannot write 90-drill.yaml"
  # /run outranks /usr/lib, where the package puts the units, and no reconcile arm writes there.
  install -d "$ROOT/run/systemd/system"
  while read -r unit; do
    [[ -n "$unit" ]] || continue
    [[ "$unit" =~ ^[A-Za-z0-9@._-]+\.(service|timer|socket)$ ]] || die "not a unit name: $unit"
    ln -sfn /dev/null "$ROOT/run/systemd/system/$unit" || die "cannot mask $unit"
  done <"$STAGE/masks"
  systemctl daemon-reload || die "daemon-reload failed"
  command -v ufw >/dev/null || apt-get install -y ufw || die "apt-get install ufw failed"
  # Added while the ruleset is empty, so it sits above the ssh limit rule the ufw arm appends and
  # the orchestrator's polling is never rate-limited. No `homelab:` comment, so the arm keeps it.
  ufw allow from "$2" to any port 22 proto tcp || die "ufw allow from $2 failed"
}

cmd_check() {
  local ok=1 pkg status units ready fw
  for pkg in homelab-tools homelab-updater homelab-daniel-box; do
    status="$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null)"
    echo "$pkg: ${status:-not installed}"
    [[ "$status" == "install ok installed" ]] || ok=0
  done
  units="$(systemctl list-unit-files 'k3s*.service' --no-legend | awk '{print $1}' | paste -sd' ')"
  echo "k3s units: ${units:-none}"
  [[ "$units" == k3s.service ]] || ok=0
  ready="$(k3s kubectl get node daniel-box \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)"
  echo "node daniel-box Ready: ${ready:-no answer}"
  [[ "$ready" == True ]] || ok=0
  fw="$(ufw status | head -n 1)"
  echo "ufw: $fw"
  [[ "$fw" == "Status: active" ]] || ok=0
  ((ok))
}

cmd_state() {
  hostname
  systemctl --failed --no-legend --no-pager
  systemctl list-unit-files 'k3s*' --no-legend --no-pager
  ufw status verbose
  k3s kubectl get nodes -o wide
  journalctl -u k3s --no-pager -n 200
  journalctl -u homelab-reconcile --no-pager -n 100
}

sub="${1:-}"
[[ -n "$sub" ]] && shift
case "$sub" in
  detached)
    case "${1:-}" in prepare | until4 | bootstrap) cmd_detached "$@" ;; *) die "usage: detached prepare|until4|bootstrap [ARGS]" ;; esac ;;
  rc) [[ $# -eq 1 ]] || die "usage: rc STEP"; cat "$STAGE/$1.rc" 2>/dev/null; true ;;
  out) [[ $# -eq 1 ]] || die "usage: out STEP"; cat "$STAGE/$1.out" ;;
  prepare) [[ $# -eq 1 ]] || die "usage: prepare SHA"; cmd_prepare "$@" ;;
  until4) bootstrap --until 4 ;;
  deviate) [[ $# -eq 2 ]] || die "usage: deviate GUEST_IP SSH_FROM"; cmd_deviate "$@" ;;
  bootstrap) bootstrap ;;
  start) systemctl enable --now k3s.service ;;
  check) cmd_check ;;
  state) cmd_state ;;
  *) die "unknown subcommand '$sub'" ;;
esac
