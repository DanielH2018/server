#!/usr/bin/env bash
# Shared setup-plane drift arms — managed by Ansible (initial_setup role); edits overwritten.
#
# Two questions about the SETUP plane, both answered against the repo checkout on this host:
#
#   arm 2  is the `copy:`-deployed code on this host byte-identical to its repo source?
#   arm 3  has the SOURCE of a `template:`-rendered script changed since this host rendered it?
#
# Arm 3 exists because arm 2 structurally cannot cover a rendered file: its on-disk form
# legitimately differs from the .j2, so a digest comparison against the source would report
# permanent drift. The comparison therefore happens one level up, on the source, against a
# sha256 the rendering run stamped (roles/setup/common/tasks/stamp_render.yml).
#
# WHY A SOURCED LIBRARY rather than two copies of these loops. manifest-prune-check.sh runs only
# on k3s server nodes (health-crons.yml is imported by the k3s role, which k3s-bringup.yml
# asserts onto k3s_server_hosts), so daniel-server — which renders the whole UPS shutdown chain —
# had no reader at all. A second implementation there would be free to drift from this one, and
# the fault these arms exist to catch is precisely two copies of a thing disagreeing. Same
# reasoning, and the same /usr/local/lib home, as kuma-push-lib.sh.
#
# Callers set REPO_DIR and then call setup_drift_scan. It sets four globals rather than printing:
#   DRIFTED  comma-joined arm-2 offenders, empty when clean
#   STALE    comma-joined arm-3 offenders, empty when clean
#   DEPLOYED_NOTE / MANIFEST_NOTE  an "unarmed" note per arm, empty when the arm has entries
# The caller owns the message wording and the push, because the two callers word them
# differently and one of them has a third arm of its own.

DEPLOYED_MANIFEST_DIR=${DEPLOYED_MANIFEST_DIR:-/var/lib/homelab/setup-deployed-manifest.d}
RENDER_MANIFEST_DIR=${RENDER_MANIFEST_DIR:-/var/lib/homelab/setup-render-manifest.d}

_check_deployed() {
  local live="$1" src="$2"
  # An unreadable SOURCE is drift, not an exemption. A pair exists ONLY because a role deployed
  # it here, so an unreadable source means the file was deleted from the repo while the artifact
  # is still live on this host. Returning 0 would swallow that, and worse, the entry still counts
  # toward DEPLOYED_ENTRIES — armed, and checking nothing.
  if [[ ! -r "$src" ]]; then
    DRIFTED="${DRIFTED:+$DRIFTED, }${live} (source gone from the repo)"
    return 0
  fi
  if [[ ! -r "$live" ]]; then
    DRIFTED="${DRIFTED:+$DRIFTED, }${live} (missing)"
    return 0
  fi
  cmp -s "$live" "$src" || DRIFTED="${DRIFTED:+$DRIFTED, }${live}"
}

# shellcheck disable=SC2034  # DRIFTED/STALE/*_NOTE are this function's outputs; the caller reads them.
setup_drift_scan() {
  DRIFTED=""
  STALE=""
  DEPLOYED_NOTE=""
  MANIFEST_NOTE=""
  local deployed_entries=0 manifest_entries=0
  local fragment live src tpl want have

  # The pairs come from a DIRECTORY of fragments, one per role, each written by the run that
  # deployed those artifacts — not from a list hardcoded here. Three paths were hardcoded until
  # 2026-08-24, so this arm was structurally incapable of seeing the other nine copy:-deployed
  # files on daniel-box (review M-5).
  #
  # `-s`, not `-r`, and the entry counters: a zero-byte fragment is readable, contributes no
  # lines, and would otherwise disarm the arm behind a confident green (review L2). An empty or
  # absent directory must read as UNARMED, never as a pass.
  for fragment in "$DEPLOYED_MANIFEST_DIR"/*; do
    [[ -s "$fragment" ]] || continue
    while read -r live src; do
      [[ -n "$live" && -n "$src" ]] || continue
      deployed_entries=$((deployed_entries + 1))
      _check_deployed "$live" "${REPO_DIR}/${src}"
    done < "$fragment"
  done
  if [[ "$deployed_entries" -eq 0 ]]; then
    DEPLOYED_NOTE="; deployed manifest absent or empty (run the setup plays once each to arm the deployed-code arm)"
  fi

  # Per-host by construction: a fragment exists only where its role ran, so a role that never
  # ran here produces no fragment rather than being stamped as covered by a host that never
  # rendered it. That is why this is not a glob over the repo.
  for fragment in "$RENDER_MANIFEST_DIR"/*; do
    [[ -s "$fragment" ]] || continue
    while read -r tpl want; do
      [[ -n "$tpl" && -n "$want" ]] || continue
      manifest_entries=$((manifest_entries + 1))
      if [[ ! -r "${REPO_DIR}/${tpl}" ]]; then
        STALE="${STALE:+$STALE, }${tpl} (template gone from the repo)"
        continue
      fi
      have=$(sha256sum "${REPO_DIR}/${tpl}" | cut -d' ' -f1)
      [[ "$have" == "$want" ]] || STALE="${STALE:+$STALE, }${tpl}"
    done < "$fragment"
  done
  if [[ "$manifest_entries" -eq 0 ]]; then
    MANIFEST_NOTE="; render manifest absent or empty (run the setup plays once each to arm the stale-script arm)"
  fi
}

# How far behind its own last update this host's checkout is, in whole days, on stdout.
#
# THE ARM ABOVE IS ONLY AS FRESH AS THE TREE IT COMPARES AGAINST, and on a host with no
# gitops-deploy that tree does not refresh itself. daniel-server was measured 39 commits behind
# origin on 2026-08-17 (see the has_gitops comment in initial_setup/tasks/crons.yml). A stale
# checkout makes arm 3 compare a stale render against the stale template it was rendered from,
# which agree — so the arm reads green precisely when the host is furthest behind. Reporting the
# tree's age alongside the verdict is what stops this fix laundering the finding it closes.
#
# Deliberately NOT a `git fetch`: this runs as root under cron against a repo owned by another
# user, and fetching would write root-owned objects into it. Age of HEAD is the signal available
# without a network call or a write, and in a repo that takes commits most days it tracks
# behind-ness closely enough to say "stop trusting the arms above".
#
# `-c safe.directory`, and why the arm was dead from the day it shipped. Running as root against
# an ubuntu-owned checkout trips git's dubious-ownership refusal, so `git log` exited non-zero,
# the `2>/dev/null` swallowed the reason, and every cron run reported "cannot read the checkout"
# — a DOWN naming the tree rather than the ownership. The hand-run that verified this arm on
# 2026-08-29 ran as ubuntu and passed; the first real cron run, on 2026-08-30 07:50, did not.
# Passed on the command line rather than written to /etc/gitconfig or the repo: the exception
# belongs to this one read, and this function is explicitly the half that writes nothing.
setup_drift_tree_age_days() {
  local head_epoch now_epoch
  head_epoch=$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" log -1 --format=%ct 2>/dev/null) || return 1
  [[ -n "$head_epoch" ]] || return 1
  now_epoch=$(date +%s)
  echo $(((now_epoch - head_epoch) / 86400))
}
