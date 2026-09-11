#!/bin/bash
# Run an interactive Ansible deploy under the locks the automated deployers take.
#
# TWO LOCKS, GUARDING TWO DIFFERENT THINGS (ADR-0017).
#
# /var/lock/server-git-tree.lock guards the git tree, and nothing else. gitops-deploy.service
# (every 10 min), the weekly secret-rotate cron and the docs refresh all take it, because each
# rewrites the tree every other deploy renders from. This wrapper holds it only long enough to
# copy HEAD into a detached worktree under /tmp/homelab-deploy-snapshots -- seconds -- then
# hands it back and runs the playbook against that snapshot. The rollout wait and the fixed
# stabilisation pause that make up most of a deploy read no tree at all, so holding the tree
# lock across them made two landings on disjoint services queue for no reason.
#
# /var/lock/server-deploy-<tag>.lock guards the CLUSTER, one lock per deploy tag, held across
# the whole playbook. Two deploys of the same service still serialize; two deploys of different
# services now run at once. A run that names no tag takes server-deploy-all.lock exclusively
# and every scoped run takes it shared, so a full run and a scoped run still exclude each other.
#
# The tree-lock wait is deliberately longer than gitops-deploy's own 180s: an interactive
# deploy should queue behind the unattended pipeline rather than give up. Note this does NOT
# protect the pipeline from a slow deploy in the other direction -- if this run holds the
# lock for more than 180s, the next gitops firing fails its unit and raises a Discord alert.
# That alert is accurate (a deploy really was in progress) and the timer retries 10 minutes
# later, so it is left as a true signal rather than suppressed. The snapshot makes that hold
# short enough that it should now be rare.
#
# A `-e target=daniel-pi` deploy takes both, even though it writes to the Pi: the tree lock
# because it renders from the local tree, and the service locks because two deploys of the
# Pi's wg-easy are as much a conflict as two deploys of sonarr.
#
# THE SNAPSHOT IS OF HEAD, NOT THE WORKING TREE. An uncommitted edit is not deployed. That is
# a real behaviour change and a deliberate one: rendered manifests are then a function of a
# commit, which is what makes the release record's `tree_dirty` structurally false and what
# makes it safe to let the tree move while the playbook runs. Commit before deploying.
#
# Usage: scripts/deploy.sh --tags "<service>" [-e target=daniel-pi] [...]
#   --check runs unlocked and un-snapshotted, from the WORKING TREE; a dry run writes nothing
#     worth serializing, and reading the working tree is what makes it useful on an edit that
#     is not committed yet.
#   --dry-run validates the k8s manifests against the live API server without applying them
#     (-e k8s_dry_run=true). Also unlocked and from the working tree, and for a stronger reason
#     than --check: it mutates nothing at all, on the cluster or on the staging tree. See
#     k8s_dry_run in inventory/group_vars/all.yml, including the 19 roles it refuses to cover.
#   --list-services prints every valid --tags value and exits.
#   --skip-tag-check deploys a tag this wrapper does not recognise.
#   --skip-staleness-check deploys from a tree behind origin/master. Refused by default
#     (exit 4, nothing deployed): a stale tree renders stale templates and reverts live
#     config, and every repo-side check reads green while it does. Being *ahead* of master
#     is normal branch work and is never refused.
#   --changed [<ref>] deploys every service touched vs <ref> (default origin/master) instead of
#     a hand-picked --tags. Resolves to --tags under the hood (scripts/deploy_tools/deploy_tags.py changed),
#     so the derived list still goes through the same lock and tag validation below, and prints
#     what it derived before doing anything else. Refuses (exit 3, nothing touched) on a broad
#     change — shared templates/inventory/setup-plane paths that don't map to one service.
#     The staleness check runs BEFORE the derivation (issue #1593): on a tree that is only
#     behind, the derived list is empty by construction and the wrapper would otherwise exit 0
#     having deployed nothing.
#   --detach backgrounds the ansible-playbook run (the ~83% of a deploy that is waiting on
#     rollout/stabilisation) and returns immediately. The staleness check, tag validation, and
#     the locks are still evaluated in THIS process before it returns, so exit 2/4 land exactly
#     as they do today; TREE-lock contention (exit 75) is checked non-blocking instead of queued
#     for LOCK_WAIT, since waiting 50 minutes before returning would defeat the point of
#     detaching — it fails fast and asks you to retry rather than sitting on the terminal. The
#     SERVICE locks queue normally even here: a service lock is held by another deploy of the
#     same service, where waiting is the right answer and the wait is bounded by that deploy.
#     Output goes to a
#     log file (path printed on return); on completion it posts to the gitops-deploy Discord
#     webhook, gated on `probe.py health <svc>` for every deployed tag that supports it.
#     Meaningless combined with --check or --dry-run (both already return immediately without
#     touching the lock) — refused with a nonzero exit rather than silently ignored.
#
# Exit codes. 0 is a finished deploy; 77, 76, 75, 4, 3 and 2 each mean NOTHING was deployed and
# each is a resume point (see the table in the repo CLAUDE.md); 20 means the playbook RAN and a task
# failed, so whatever applied before it is live. Nothing else is returned — ansible-playbook's
# own status collides with 2/3/4 and is collapsed onto 20, see PLAYBOOK_FAILED below.

set -u

# Ansible's cli/__init__.py refuses to start on a non-blocking stdout or stderr
# ("ERROR: Ansible requires blocking IO on stdin/stdout/stderr"), and Claude Code's Bash
# tool hands its child a regular file with O_NONBLOCK set. The .claude/hooks/uv-python.sh
# fixup cannot reach the `uv run ansible-playbook` inside this script — it only rewrites
# the command text a session types — so the restore is repeated here. O_NONBLOCK lives on
# the open file description that fork/exec shares, so clearing it from this child clears
# it for the ansible run below; on a terminal or under systemd the flag is already clear
# and this is a no-op. `3>&2` hands the child the real stderr past the `2>/dev/null` that
# keeps it quiet — without it the flag would be cleared on /dev/null instead.
python3 -c 'import os; [os.set_blocking(f, True) for f in (0, 1, 3)]' 3>&2 2>/dev/null || true

# The git-tree lock (ADR-0011). Overridable for the same reason the two paths below are: the
# concurrency tests take this lock for real, and taking the production one would queue a live
# gitops tick behind a test -- and be queued behind one.
LOCK="${HOMELAB_DEPLOY_TREE_LOCK:-/var/lock/server-git-tree.lock}"
# Where the per-service locks live. One `server-deploy-<tag>.lock` per deploy tag, plus
# `server-deploy-all.lock` for a run that names no tag. These are what serialize the CLUSTER
# work, which is nearly all of a deploy's wall clock; the tree lock above is held only for the
# snapshot. Overridable so the bash-level tests can take real flocks on a tmp_path.
LOCK_DIR="${HOMELAB_DEPLOY_LOCK_DIR:-/var/lock}"
# Where a run's detached snapshot worktree is created. Overridable for the same reason.
SNAPSHOT_ROOT="${HOMELAB_DEPLOY_SNAPSHOT_ROOT:-/tmp/homelab-deploy-snapshots}"
# Covers gitops-deploy's worst-case hold of 2940s (STAGING_GATE_TIMEOUT_S 600 +
# STAGING_EXPECT_TIMEOUT_S 120 + K8S_DEPLOY_TIMEOUT_S 900 + K8S_ROLLBACK_TIMEOUT_S 1320), not its
# TimeoutStartSec. Was 1500 from d1a5b6c9 until 2026-08-23, when the unit's TimeoutStartSec really
# was 25min; it then went 25 -> 35 -> 45min and this value was left behind, so a deploy launched
# during a pathological gitops run gave up having deployed nothing while the run it was queued
# behind was still legitimately working. Derived from the same role defaults the deployer reads
# and pinned by test_deploy_lock_wait_budget.py, so raising any of the four fails that test
# rather than silently shortening this wait again. It is also the budget each per-service lock
# waits: a service lock is held across the same playbook the tree lock used to cover.
LOCK_WAIT=3000
LOCK_BUSY=75
# flock failed for a reason that is NOT contention -- a bad descriptor (65), a lock file this
# user cannot open (1), anything else it returns. Nothing was deployed, exactly as 75 promises,
# but the remedy differs: retrying changes nothing until the file itself is fixed. It has its
# own code because both are refusals and only one clears on its own. Until 2026-09-11 every
# such failure fell through to PLAYBOOK_FAILED below, so land.sh read "a task failed AFTER
# applying; some changes are live" for a run that never started ansible at all (issue #1775).
LOCK_UNAVAILABLE=76
# The playbook ran and a task failed. Distinct from every code above because those all mean
# NOTHING was deployed, while this one means the opposite: a play that reaches PLAY RECAP with
# failed=1 has already applied whatever ran before the failing task.
#
# It exists because ansible-playbook's own exit codes COLLIDE with this wrapper's. Ansible
# returns 2 for "one or more hosts failed", 3 for "hosts unreachable" and 4 for a parse error;
# this script reserves 2 for a tag miss, 3 for a broad change and 4 for a stale tree. Until
# 2026-09-02 the final `exit "$status"` handed ansible's number straight out, so a play that
# failed on a post-apply assert exited 2 and every consumer read it as the tag miss. That is
# issue #840: land.sh printed `deploy-failed (... a derived tag matched no service, so nothing
# deployed; tags: terraria,uptime-kuma)` for a run whose manifests both applied and which failed
# in k8s/rollout-drain. 20 is outside {0,1,2,3,4,64,75}, so the two can never be confused again.
PLAYBOOK_FAILED=20
# The snapshot worktree could not be created, so there was no tree to render from and NOTHING
# was deployed. Its own code rather than 76's, because the remedy differs: 76 is the lock file,
# this is the snapshot root or the git object store. Both are environment faults a retry alone
# does not clear, which is why neither collapses onto the contention code.
SNAPSHOT_FAILED=77

# Record a successful deploy where Grafana can draw it as a dashboard annotation.
#
# WHY A LOG LINE and not a POST to Grafana's /api/annotations. Grafana has no hostPort and no
# pinned ClusterIP, and this script is a HOST process, so calling into the cluster would mean
# either pinning a fourth address or routing through Traefik with a standing write credential.
# Neither is needed: the Alloy shipper already tails /var/log/syslog into loki-homelab on both nodes (the
# same path the host crons' `status=down` lines take to the alert-history board), and Grafana
# already reads that Loki by Service DNS. So the deployer writes locally and the cluster reads —
# no address, no credential, no new component.
#
# It also puts the record somewhere that SURVIVES. grafana-data is on longhorn-nobackup, so
# annotations stored in Grafana's own database have no offsite copy; reconstructing them from
# Loki matches the existing decision that Grafana holds nothing worth backing up.
#
# Fire-and-forget by construction: `|| true` and a discarded stderr. An annotation is a
# convenience, and a deploy that actually succeeded must never report failure because logging
# it did not.
emit_deploy_annotation() {
    local status="$1"
    [[ "$status" == 0 ]] || return 0

    local label
    label=$(
        IFS=,
        echo "${tags[*]:-full}"
    )
    # The snapshot's own commit, captured when the snapshot was made rather than read here.
    # This runs after the snapshot is gone, and on --detach it runs long after the working
    # tree may have moved on; re-reading git would annotate a commit this run never deployed.
    local sha="${snapshot_sha:-}"
    [[ -n "$sha" ]] || sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

    # logfmt, so a Grafana annotation query can pull `services` out as the annotation text
    # rather than showing the whole raw line.
    logger -t deploy-annotation \
        "event=deploy services=${label} sha=${sha} result=ok" \
        2>/dev/null || true
}

# ── the snapshot ──────────────────────────────────────────────────────────────────────────
#
# WHY A SNAPSHOT. The tree lock guards the git tree (ADR-0011), and rendering is the only part
# of a deploy that reads it. Rendering is a few seconds; the rollout wait and the fixed
# stabilisation pause that follow are minutes, and they touch no tree at all. Copying HEAD into
# a detached worktree lets this run render from bytes nothing else can move, so the tree lock
# is released before the playbook starts and two landings on disjoint services stop queueing
# behind each other. ADR-0017 holds the full reasoning.

# The snapshot this run created, empty until `make_snapshot` succeeds, and the commit it holds.
snapshot=""
snapshot_sha=""
# The descriptor this run holds its snapshot's owner lock on. See OWNER_LOCK below.
snapshot_owner_fd=""

# The advisory lock that says a snapshot is still in use, inside the snapshot itself.
#
# DECIDED: ownership is this lock, never the pid in the directory name. `--detach` runs its
# playbook in a backgrounded subshell whose `$$` is still the PARENT's pid, and the parent
# exits as soon as it has backgrounded the job -- so for the whole life of a detached deploy
# the name's pid is dead, and a pid-based reaper (even one run by `--check`) deleted the
# worktree out from under the running playbook. A process cannot lie about holding a flock.
# (ADR-0017)
OWNER_LOCK=.deploy-owner.lock

# Remove this run's snapshot. Safe to call twice and safe to call having made none.
#
# `git worktree remove --force` deregisters the worktree itself, so no prune is needed here --
# and an unconditional one would deregister other sessions' worktrees whose directories are
# temporarily missing. The reaper below prunes, gated on having reaped something.
remove_snapshot() {
    [[ -n "$snapshot" ]] || return 0
    if [[ -n "$snapshot_owner_fd" ]]; then
        exec {snapshot_owner_fd}>&-
        snapshot_owner_fd=""
    fi
    git worktree remove --force "$snapshot" >/dev/null 2>&1 || rm -rf "$snapshot"
    snapshot=""
}

# Remove snapshots no deploy is using any more. CALL ONLY WHILE HOLDING THE TREE LOCK.
#
# A run killed outside its trap -- SIGKILL, a reboot, an OOM -- leaves both the directory and
# git's registration of it. A live one is told apart by its owner lock: the process running the
# playbook holds `<snapshot>/.deploy-owner.lock`, so `flock -n` on it fails while that deploy is
# alive and succeeds the moment it is not, however it died. The pid in the directory name is for
# a human reading `ls`; it decides nothing.
#
# The tree lock is what closes the window between `git worktree add` creating the directory and
# `make_snapshot` flocking the owner lock inside it. For those few seconds the directory looks
# ownerless, and an unlocked reaper -- a concurrent `--check`, which needs no lock of its own --
# deleted the worktree a deploy was about to render from, then told the operator "retrying alone
# will not fix either". Both snapshot-taking arms reap under the same lock they snapshot under;
# `--check` and `--dry-run` take no tree lock and so reap nothing, which costs only the next
# locked invocation's sweep.
#
# Fails open, like the fact-cache preflight above it: a snapshot this run cannot reap costs
# disk, and refusing every deploy over that would be the worse failure.
reap_dead_snapshots() {
    [[ -d "$SNAPSHOT_ROOT" ]] || return 0
    local dir reaped=0
    for dir in "$SNAPSHOT_ROOT"/*; do
        [[ -d "$dir" ]] || continue
        # `true` under the lock: this only asks whether the lock is free. flock releases it
        # when that command exits, so nothing is held across the removal below.
        flock -n "$dir/$OWNER_LOCK" true >/dev/null 2>&1 || continue
        git worktree remove --force "$dir" >/dev/null 2>&1 || rm -rf "$dir"
        reaped=1
    done
    # Only after reaping something. `git worktree prune` deregisters every worktree whose
    # directory is missing, other sessions' included, so an unconditional prune on every
    # deploy would collect trees this run has nothing to do with.
    [[ "$reaped" == 0 ]] || git worktree prune >/dev/null 2>&1 || true
}

# Copy HEAD into a detached worktree under SNAPSHOT_ROOT; sets `snapshot` and `snapshot_sha`.
#
# HEAD, not the working tree: a deploy now renders the commit, so an uncommitted edit is NOT
# deployed. That is the cost ADR-0017 accepts, and it is what makes `tree_dirty` in the release
# record structurally false. Called while the tree lock is held, which is the whole reason the
# lock still exists.
#
# `--detach` so the snapshot claims no branch. A worktree on a branch would make that branch
# unusable everywhere else, and `scripts/dev/prune_worktrees.py` keeps any detached worktree
# rather than classifying it.
make_snapshot() {
    local stamp dir fd
    stamp=$(date +%Y%m%d-%H%M%S)
    dir="$SNAPSHOT_ROOT/${tag_label//[^A-Za-z0-9_.,-]/_}-$stamp-$BASHPID"
    mkdir -p "$SNAPSHOT_ROOT" || return 1
    git worktree add --detach "$dir" HEAD >/dev/null 2>&1 || return 1
    snapshot="$dir"
    snapshot_sha=$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo unknown)
    # The owner lock, held until this run is done with the snapshot. On --detach the
    # backgrounded subshell inherits this descriptor and the parent closes its copy, so the
    # lock follows the playbook rather than the shell that created the directory -- flock
    # releases only when every descriptor on the open file description is closed.
    # `-x` is flock's default and is spelled out here: it is what tells this acquire apart from
    # the tree lock's own `flock -n <fd>` probe, for a reader and for the bash tests' stubs.
    if ! exec {fd}>"$dir/$OWNER_LOCK" || ! flock -n -x "$fd"; then
        remove_snapshot
        return 1
    fi
    snapshot_owner_fd="$fd"
}

# Hand the snapshot to the backgrounded subshell that inherited its descriptors: forget it here
# so the parent's EXIT trap leaves it alone, and close the parent's copy of the owner lock so
# the subshell is its only holder.
disown_snapshot() {
    if [[ -n "$snapshot_owner_fd" ]]; then
        exec {snapshot_owner_fd}>&-
        snapshot_owner_fd=""
    fi
    snapshot=""
}

# Run the deploy playbook against the snapshot. The ONE place a locked run invokes ansible.
#
# UV_PROJECT_ENVIRONMENT points at the CALLING checkout's .venv. `uv run` resolves its project
# from the working directory, and a snapshot carries no .venv, so without this every deploy
# would build a fresh environment inside a directory deleted minutes later -- and the Ansible
# fact cache, which is keyed by host and shared across checkouts, would be pinned to an
# interpreter path that then disappears. That is the exact failure `fact_cache_guard.py`
# exists to clean up after. Measured 2026-09-11 from a detached worktree: with this set,
# `uv run python -c 'print(sys.prefix)'` reports the caller's .venv and creates nothing.
run_playbook_in_snapshot() {
    (
        cd "$snapshot" || exit 1
        UV_PROJECT_ENVIRONMENT="$repo_root/.venv" \
            uv run ansible-playbook ansible/deploy.yml "$@"
    )
}

# Every deploy tag this run must lock, read from the SNAPSHOT. Sets `full_run_tags`.
#
# Read from the snapshot rather than the working tree, and while the tree lock is still held:
# a full run locks one tag per declared service, and the list has to come from the bytes it is
# about to deploy. Reading it later, unlocked, would let a concurrent pull add a service
# between the enumeration and the deploy -- which would then roll out under no lock at all.
#
# An empty list is a FAILURE, not a run with nothing to lock: `deploy_tags.py list` prints one
# line per containers_list entry, so nothing at all means it did not run.
full_run_tags=()
enumerate_full_run_tags() {
    local tag
    full_run_tags=()
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        full_run_tags+=("$tag")
    done < <(
        cd "$snapshot" || exit 1
        UV_PROJECT_ENVIRONMENT="$repo_root/.venv" \
            uv run python scripts/deploy_tools/deploy_tags.py list 2>/dev/null |
            LC_ALL=C sort -u
    )
    [[ ${#full_run_tags[@]} -gt 0 ]]
}

say_tag_enumeration_failed() {
    echo "deploy: could not list the deploy tags from the snapshot -- nothing was deployed." >&2
    echo "  A run with no --tags locks one lock per declared service, so an unreadable list" >&2
    echo "  means it would deploy everything holding nothing. Check that" >&2
    echo "  'uv run python scripts/deploy_tools/deploy_tags.py list' works in this checkout." >&2
}

say_snapshot_failed() {
    echo "deploy: could not snapshot HEAD into $SNAPSHOT_ROOT -- nothing was deployed." >&2
    echo "  The playbook renders from a detached worktree of HEAD, so without one there is" >&2
    echo "  nothing to deploy from. Check the directory is writable and that" >&2
    echo "  'git worktree add --detach' works here; retrying alone will not fix either." >&2
}

# ── the per-service locks ─────────────────────────────────────────────────────────────────
#
# DECIDED: the lock order is `server-deploy-all.lock` first -- shared for a scoped run,
# exclusive for a full one -- then each tag's own lock in sorted order. Sorted order is what
# makes two overlapping scoped runs deadlock-free; taking `all` before any tag is what makes a
# full run and a scoped run deadlock-free. Across the two deploy paths: this wrapper takes the
# tree lock, snapshots, RELEASES the tree lock and only then takes service locks, while the
# GitOps deployer takes the tree lock and holds it across its service locks. There is no cycle
# because this wrapper never re-takes the tree lock after releasing it. (ADR-0017)

# The descriptors this run holds service locks on. Held for the life of the shell that runs the
# playbook; on --detach the background subshell inherits them and the parent's copies close.
service_lock_fds=()

# Take one service lock, blocking up to LOCK_WAIT. Returns flock's status.
take_service_lock() {
    local label="$1" path="$2" mode="$3" fd started waited status
    local flock_args=(-w "$LOCK_WAIT" -E "$LOCK_BUSY")
    [[ "$mode" != shared ]] || flock_args=(-s "${flock_args[@]}")
    # Its own message and its own code: flock never ran, so reporting this as "flock exit 1"
    # sends an operator to look at a lock nobody took.
    if ! exec {fd}>"$path"; then
        echo "deploy: could not open the service lock file $path -- nothing was deployed." >&2
        echo "  flock never ran. $LOCK_DIR must exist and be writable by this user" >&2
        echo "  (ls -ld $LOCK_DIR); retrying changes nothing until it is." >&2
        return "$LOCK_UNAVAILABLE"
    fi
    service_lock_fds+=("$fd")
    started=$SECONDS
    flock "${flock_args[@]}" "$fd"
    status=$?
    waited=$((SECONDS - started))
    [[ "$status" == 0 ]] || return "$status"
    # Silent at 0s, for the reason the tree lock's line is: a line on every deploy buries the
    # ones that mean something. land.py books these seconds into the landing's `lock=` field
    # (land_lib/tools.py:in_flock_wait), the same as the tree lock's.
    if [[ "$waited" -gt 0 ]]; then
        echo "deploy: service lock $label acquired after ${waited}s" >&2
    fi
}

# Take every service lock this run needs, in the order the DECIDED note above fixes.
take_service_locks() {
    local tag
    if [[ ${#split_tags[@]} -gt 0 ]]; then
        # Shared on `all`: scoped runs do not exclude each other, but a full run does.
        take_service_lock all "$LOCK_DIR/server-deploy-all.lock" shared || return $?
        while read -r tag; do
            [[ -n "$tag" ]] || continue
            take_service_lock "$tag" "$LOCK_DIR/server-deploy-${tag//[^A-Za-z0-9_.-]/_}.lock" \
                exclusive || return $?
        done < <(printf '%s\n' "${split_tags[@]}" | LC_ALL=C sort -u)
        return 0
    fi
    # A run with no tags deploys everything, so it excludes every scoped run through `all`
    # AND takes each declared tag's own lock. The second half is redundant against a scoped
    # run, which also takes `all`; it is what stops a full run from starting while some other
    # actor holds a single tag's lock without `all`.
    take_service_lock all "$LOCK_DIR/server-deploy-all.lock" exclusive || return $?
    for tag in "${full_run_tags[@]}"; do
        take_service_lock "$tag" "$LOCK_DIR/server-deploy-${tag//[^A-Za-z0-9_.-]/_}.lock" \
            exclusive || return $?
    done
}

# Drop every service lock this shell holds. Closing the descriptor releases the lock.
release_service_locks() {
    local fd
    for fd in "${service_lock_fds[@]-}"; do
        [[ -n "$fd" ]] || continue
        exec {fd}>&-
    done
    service_lock_fds=()
}

# What flock said when it failed for a reason other than contention. Both lock paths print
# this, because both can hit it and an operator reading one log must not have to know which
# arm ran. It names flock's own number: 65 is a bad descriptor and 1 is a lock file this user
# cannot open, and those want different fixes.
say_lock_unavailable() {
    local path="${2:-$LOCK}" what=file
    # The service-lock arm passes LOCK_DIR, so this names a directory there. "Check the lock
    # file /var/lock exists" sent an operator looking for a file that is not supposed to be one.
    [[ ! -d "$path" ]] || what=directory
    echo "deploy: could not take $path (flock exit $1) -- nothing was deployed." >&2
    echo "  This is NOT contention: no deploy is holding the lock, flock itself failed." >&2
    echo "  Check the lock $what $path exists and is writable by this user" >&2
    echo "  (ls -ld $path). Retrying changes nothing until it is; nothing ran, so a" >&2
    echo "  re-run is safe once fixed." >&2
}

# The tree lock's holder, in the shape land_lib/tools.py:lock_holder returns, or "" when
# nobody holds it. fuser prints the holding PIDs on stdout and the path on stderr; the lowest
# PID is the flock parent, whose children inherited the descriptor. The two sources write the
# same `holder="..."` field on the Landings board, so they format it the same way.
read_lock_holder() {
    local pid detail
    pid=$(fuser "$LOCK" 2>/dev/null |
        awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) print $i }' |
        sort -n | head -1)
    [[ -n "$pid" ]] || return 0
    detail=$(ps -o etimes=,args= -p "$pid" 2>/dev/null |
        tr -s '[:space:]' ' ' | sed 's/^ *//; s/ *$//')
    printf 'pid %s (etimes, command): %s' "$pid" "$detail"
}

# One line naming what the tree lock cost this run: for an operator reading the log, and for
# land.py, which parses it into the landing's `lock=` field (land_lib/tools.py:in_flock_wait).
say_lock_acquired() {
    local waited="$1" holder="$2" line
    line="deploy: lock acquired after ${waited}s"
    [[ -z "$holder" ]] || line="$line (holder was: $holder)"
    echo "$line" >&2
}

# The checkout this session is working in, not the primary one — a session in a worktree
# has always deployed its own tree, and running the wrapper must not change that.
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root" || exit 1

# --changed [<ref>] is resolved to --tags "<derived>" right here, before anything else looks at
# "$@" — everything below (locking, --check/--dry-run detection, tag validation) then runs
# exactly the --tags path it always has, unchanged. Pulled out as its own pass because the ref
# is an OPTIONAL positional: the single-pass case-statement loop below (which handles --tags'
# own required argument via next_is_tags) can't tell "no ref given" from "the next flag" without
# look-ahead, so --changed gets one.
raw_args=("$@")
filtered_args=()
i=0
n=${#raw_args[@]}
changed_requested=0
changed_ref="origin/master"
pre_skip_staleness=0
while [[ "$i" -lt "$n" ]]; do
    a="${raw_args[$i]}"
    if [[ "$a" == "--changed" ]]; then
        changed_requested=1
        i=$((i + 1))
        if [[ "$i" -lt "$n" && "${raw_args[$i]}" != -* ]]; then
            changed_ref="${raw_args[$i]}"
            i=$((i + 1))
        fi
        continue
    fi
    if [[ "$a" == "--skip-staleness-check" ]]; then
        pre_skip_staleness=1
    fi
    filtered_args+=("$a")
    i=$((i + 1))
done
set -- "${filtered_args[@]}"

# The staleness gate, hoisted into a function so the --changed pass below can ask it FIRST
# (issue #1593). It runs once per invocation whichever call site gets there first:
# staleness_checked makes the second call a no-op, so --changed pays no second fetch.
staleness_checked=0
# The comma-joined --tags this run deploys, filled by the tag split below. The gate asks a
# narrower question when it is set: a commit in HEAD..origin/master reaching none of these tags
# and no broad path cannot revert what this deploy renders, and the GitOps deployer
# fast-forwards to the newest GREEN commit rather than to the tip, so the primary checkout is
# legitimately behind a pending tip while every landing deploys from it.
split_tags_csv=""
staleness_gate() {
    if [[ "$staleness_checked" == 1 ]]; then
        return 0
    fi
    staleness_checked=1
    # The --changed call site reaches this before any tag has been derived, so it asks the
    # unscoped question. That is the right answer there: the derivation reads the same stale
    # tree, so there is no tag list to narrow by yet.
    if [[ -n "$split_tags_csv" ]]; then
        uv run python scripts/deploy_tools/deploy_staleness.py --tags "$split_tags_csv" || exit 4
    else
        uv run python scripts/deploy_tools/deploy_staleness.py || exit 4
    fi
}

if [[ "$changed_requested" == 1 ]]; then
    # Asked BEFORE the derivation, for the reason issue #1566 put it before tag validation: a
    # question asked of a stale tree answers about the wrong tree. Here the wrong answer is
    # also the quietest one. On a checkout that is only BEHIND -- every commit of its own
    # already merged -- the three-dot range the derivation uses is empty by construction, so
    # it derives NO tags and the wrapper exits 0 having deployed nothing. Exit 0 is the one
    # code no consumer treats as a resume point, so a stale tree answered "nothing to deploy"
    # as a success. Exit 4 is the honest answer, and land.sh already retries it.
    if [[ "$pre_skip_staleness" == 0 ]]; then
        staleness_gate
    fi
    derived_tags=$(uv run python scripts/deploy_tools/deploy_tags.py changed "$changed_ref")
    status=$?
    if [[ "$status" != 0 ]]; then
        exit "$status"
    fi
    if [[ -z "$derived_tags" ]]; then
        exit 0
    fi
    set -- "$@" --tags "$derived_tags"
fi

# Parse the wrapper's own flags out of "$@". Everything this loop collects is consumed below:
# `tags` by the tag validation (which runs after the staleness check, see there), and
# `--skip-tag-check`/`--skip-staleness-check`/`--dry-run`/`--detach` by their own gates. The
# wrapper flags are stripped from `args`, so they never reach ansible-playbook.
args=()
tags=()
next_is_tags=0
skip_tag_check=0
skip_staleness_check=0
dry_run=0
detach=0

for arg in "$@"; do
    if [[ "$next_is_tags" == 1 ]]; then
        next_is_tags=0
        tags+=("$arg")
        args+=("$arg")
        continue
    fi
    case "$arg" in
        --skip-tag-check)
            skip_tag_check=1
            ;;
        --skip-staleness-check)
            skip_staleness_check=1
            ;;
        --detach)
            detach=1
            ;;
        --dry-run)
            # Translated, not passed through: ansible-playbook has no --dry-run of its own
            # (--check is the Ansible-level one, and it is a different mode entirely).
            dry_run=1
            args+=(-e k8s_dry_run=true)
            ;;
        --list-services)
            exec uv run python scripts/deploy_tools/deploy_tags.py list
            ;;
        --tags | -t)
            next_is_tags=1
            args+=("$arg")
            ;;
        --tags=*)
            tags+=("${arg#--tags=}")
            args+=("$arg")
            ;;
        *)
            args+=("$arg")
            ;;
    esac
done

set -- "${args[@]}"

# Ansible accepts comma-separated tags in one argument (--tags "a,b"), so split each argument
# into the single tags both the staleness gate and the tag validation below ask about. Done
# here, once, because the gate runs before the validation and both need the split list.
# An explicit IFS swap around the expansion rather than a prefix assignment on `read`, whose
# effect on a herestring expansion is not worth relying on.
split_tags=()
old_ifs=$IFS
for tag_arg in "${tags[@]-}"; do
    [[ -n "$tag_arg" ]] || continue
    IFS=','
    # shellcheck disable=SC2086  # unquoted on purpose: this IS the comma split
    for tag in $tag_arg; do
        split_tags+=("$tag")
        split_tags_csv="${split_tags_csv:+$split_tags_csv,}$tag"
    done
    IFS=$old_ifs
done

# The tags this run deploys, as one filesystem-safe word. Names both the --detach log and the
# snapshot directory, so an operator reading either can tell which run left it.
tag_label=$(
    IFS=,
    echo "${split_tags[*]:-full}"
)

# --detach + --check/--dry-run is meaningless: both of those already return immediately without
# touching the lock, so there is nothing to background. Checked here, right after args are known
# and before the (comparatively slow) staleness check, so a nonsensical combination fails fast
# rather than doing something surprising. `--changed` is the one path where the staleness check
# already ran (see the hoist above), so there the stale tree is reported first, at exit 4.
check_requested=0
for arg in "$@"; do
    if [[ "$arg" == "--check" ]]; then
        check_requested=1
        break
    fi
done
if [[ "$detach" == 1 && ( "$check_requested" == 1 || "$dry_run" == 1 ) ]]; then
    echo "deploy: --detach with --check or --dry-run is meaningless -- both already return" >&2
    echo "  immediately without touching the lock, so there is nothing to background." >&2
    exit 64
fi

# The fact cache is shared by host across every worktree on this machine, and it pins the
# interpreter of whichever session gathered facts first. A cache naming a worktree that is gone
# fails EVERY deploy at Gathering Facts for the full 7200s TTL, with an error that names a module
# rather than the cache -- and it does so AFTER the ~9 minute wait on the lock below, so the run
# looks like it built for ten minutes and then died having done nothing. Clearing costs one
# re-gather, so this clears rather than refuses. Runs before --check and --dry-run too: a dry run
# gathers facts like any other run, which is how the cache gets re-poisoned in the first place.
#
# DECIDED: this preflight fails OPEN. It is a remediation, not a verdict -- if it cannot clear the
# cache, the deploy proceeds and dies at Gathering Facts exactly as it does today, except now with
# this script's stderr naming the cache directly above the misleading module error. Blocking every
# deploy on a bug in a cache-cleaner would be a worse failure than the one it prevents.
uv run python scripts/deploy_tools/fact_cache_guard.py --clear || true

# A tree behind origin/master renders stale templates and reverts live config for the roles
# it targets, while every repo-side check still reads green -- the stale tree is consistent
# with itself. Measured 2026-08-19; see scripts/deploy_tools/deploy_staleness.py. This runs before --check
# and --dry-run too: a green dry run against a stale tree is the misleading signal itself.
if [[ "$skip_staleness_check" == 0 ]]; then
    staleness_gate
fi

# Tag validation runs AFTER the staleness check on purpose (issue #1566). Both refusals mean
# nothing was deployed, but they name different causes, and a tag check against a stale tree
# answers about the wrong tree: `deploy_tags.py validate` reads the checkout's own
# containers_list, so the first landing of a NEW role reads as a tag miss (exit 2, "your change
# broke something") whenever the tick has not yet fast-forwarded the merge commit. Exit 4 is the
# honest answer there -- it names the stale tree, and `land.sh` already retries a stale tree
# while it reports a tag miss as a failed deploy.
#
# Ansible exits 0 on a tag that matches nothing, so a typo'd service name deploys nothing and
# reports success -- see scripts/deploy_tools/deploy_tags.py for why the play behaves that way.
# This still runs before the lock and before --check/--dry-run, since a dry run against a
# nonexistent tag is just as misleading. --skip-tag-check bypasses, and is stripped in the
# parse above so it never reaches ansible-playbook.
if [[ "$skip_tag_check" == 0 && ${#split_tags[@]} -gt 0 ]]; then
    if ! uv run python scripts/deploy_tools/deploy_tags.py validate "${split_tags[@]}"; then
        exit 2
    fi
fi

for arg in "$@"; do
    if [[ "$arg" == "--check" ]]; then
        exec uv run ansible-playbook ansible/deploy.yml "$@"
    fi
done

# Same reasoning as --check, and a stronger case for it: a k8s dry run renders to a temp dir it
# then deletes and applies with --dry-run=server, so it writes neither the cluster nor the
# staging tree. Nothing for the lock to serialize against.
if [[ "$dry_run" == 1 ]]; then
    exec uv run ansible-playbook ansible/deploy.yml "$@"
fi

if [[ "$detach" == 1 ]]; then
    # The lock is still taken HERE, synchronously -- only the ansible-playbook run itself (the
    # ~83% of a deploy spent waiting on rollout/stabilisation) moves to the background. Waiting
    # up to LOCK_WAIT (45min) before returning would defeat the point of --detach, so contention
    # is checked non-blocking: exit 75 means "try again shortly", not "waited 45 minutes then
    # gave up" the way it does without --detach.
    log_dir=/tmp/homelab-deploy-logs
    mkdir -p "$log_dir"
    log="$log_dir/deploy-${tag_label//[^A-Za-z0-9_.,-]/_}-$(date +%Y%m%d-%H%M%S)-$$.log"

    exec {lockfd}>"$LOCK"
    # `-E "$LOCK_BUSY"` for the same reason the queued path passes it: without it `flock -n`
    # answers 1 for a held lock AND for a lock file it could not open, and this arm would
    # report a deploy in progress for a broken descriptor. Measured 2026-09-11 against real
    # flock, two descriptors on one file in one shell: `flock -n -E 75` on the second exits
    # 75, so `-E` does apply to `-n` and contention still routes to LOCK_BUSY below.
    flock -n -E "$LOCK_BUSY" "$lockfd"
    detach_lock_status=$?
    if [[ "$detach_lock_status" != 0 && "$detach_lock_status" != "$LOCK_BUSY" ]]; then
        say_lock_unavailable "$detach_lock_status"
        exit "$LOCK_UNAVAILABLE"
    fi
    if [[ "$detach_lock_status" != 0 ]]; then
        echo "deploy --detach: could not take $LOCK right now -- nothing was deployed." >&2
        echo "  A deploy is already running. Likely holders: gitops-deploy.service" >&2
        echo "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron," >&2
        echo "  or another Claude session (uv run python scripts/dev/prune_worktrees.py)." >&2
        echo "  --detach fails fast on contention rather than queuing for ${LOCK_WAIT}s --" >&2
        echo "  retry shortly, or drop --detach to queue normally." >&2
        exit "$LOCK_BUSY"
    fi
    # Always 0s here by construction: `flock -n` either takes the lock at once or refuses, so
    # this path never queues. Printed anyway, so the log shape does not depend on which path
    # ran and land.py books the same field from both.
    say_lock_acquired 0 ""

    # Under the tree lock, and the only thing this arm needs it for.
    reap_dead_snapshots
    if ! make_snapshot; then
        flock -u "$lockfd"
        exec {lockfd}>&-
        say_snapshot_failed
        exit "$SNAPSHOT_FAILED"
    fi
    # Still under the tree lock: a full run's tag list has to be read from the snapshot before
    # anything else can move the tree.
    if [[ ${#split_tags[@]} -eq 0 ]] && ! enumerate_full_run_tags; then
        remove_snapshot
        flock -u "$lockfd"
        exec {lockfd}>&-
        say_tag_enumeration_failed
        exit "$SNAPSHOT_FAILED"
    fi
    flock -u "$lockfd"
    exec {lockfd}>&-

    # The service locks WAIT, unlike the tree lock above. --detach fails fast on the tree lock
    # because that lock is held by unrelated work (the tick, the rotate cron) a caller can do
    # nothing about; a service lock is held by another deploy of the SAME service, where
    # queueing is the correct behaviour and the wait is bounded by the deploy it is behind.
    if ! take_service_locks; then
        service_lock_status=$?
        release_service_locks
        remove_snapshot
        if [[ "$service_lock_status" == "$LOCK_BUSY" ]]; then
            echo "deploy --detach: a deploy of one of these services held its lock for the" >&2
            echo "  full ${LOCK_WAIT}s -- nothing was deployed. Retry." >&2
        elif [[ "$service_lock_status" == "$LOCK_UNAVAILABLE" ]]; then
            # take_service_lock already said which file it could not open.
            exit "$LOCK_UNAVAILABLE"
        else
            say_lock_unavailable "$service_lock_status" "$LOCK_DIR"
            exit "$LOCK_UNAVAILABLE"
        fi
        exit "$LOCK_BUSY"
    fi

    (
        # The subshell installs its own cleanup: bash resets traps to their default in a
        # backgrounded subshell, and the PARENT's EXIT trap fires the moment it backgrounds
        # this one -- which would delete the snapshot out from under the playbook.
        trap remove_snapshot EXIT
        run_playbook_in_snapshot "$@" >"$log" 2>&1
        run_status=$?
        remove_snapshot
        release_service_locks
        # Annotated from inside the subshell, where the run actually finished — the parent
        # returned at exit 0 the moment it backgrounded this, long before there was anything
        # to record.
        emit_deploy_annotation "$run_status"
        # shellcheck disable=SC2094  # false positive: the notifier only receives $log as a
        # path string (to mention in its Discord post) and never opens it itself -- the only
        # actual writer of the file is this append redirect.
        uv run python scripts/deploy_tools/deploy_detach_notify.py \
            --status "$run_status" \
            --log "$log" \
            --tags "$(
                IFS=,
                echo "${split_tags[*]-}"
            )" \
            >>"$log" 2>&1
    ) &
    bg_pid=$!
    disown "$bg_pid" 2>/dev/null || true
    release_service_locks
    # The background subshell owns the snapshot now, so this shell must not reap it on the way
    # out and must stop holding its owner lock -- while the subshell's inherited descriptor
    # keeps that lock held, which is what stops the next invocation's reaper collecting a
    # directory the playbook is still reading.
    disown_snapshot

    echo "deploy --detach: running in background (pid $bg_pid)."
    echo "  log:  $log"
    echo "  tail: tail -f $log"
    echo "  Posts to the gitops-deploy Discord webhook when it settles, gated on" \
        "'probe.py health <svc>' for every deployed tag that supports it."
    exit 0
fi

# The lock is taken on a DESCRIPTOR rather than through `flock <file> <command>`, so that the
# wait can be timed separately from the run. Inside the command form a 20-minute queue and a
# 20-minute playbook are the same number, and every landing booked the queue as deploy time:
# `lock=0` on every ledger row for the 14 days to 2026-09-11, while the lock was busy 17% of
# one of them. Behaviour is unchanged -- the same LOCK_WAIT budget, the same LOCK_BUSY exit.
#
# SAMPLED BEFORE THIS SHELL OPENS THE LOCK FILE, which is the only order that can name anyone
# else. fuser scans every process's descriptors, so once this shell holds one it reports
# ITSELF -- and closing the descriptor for the read does not help, because the process fuser
# finds is the parent that still holds it. Sampling afterwards named the landing as its own
# blocker whenever the real holder released during the wait. That costs a fuser and a ps on
# every deploy, uncontended ones included, which is what land_lib's `retry_while_locked`
# already pays per attempt for the same reason.
lock_holder_seen=$(read_lock_holder)

# Opened for WRITING, as the --detach branch above already opens it. flock(1) opens the same
# file read-only, so this needs write permission where the command form did not: the file is
# created by whichever of the deploy user and gitops-deploy.service takes it first, and both
# run as sys_user.
exec {lockfd}>"$LOCK"
lock_started=$SECONDS
lock_taken=0
flock_status=0
# 0 until the service-lock phase runs, so the refusal arms below can read it unconditionally.
service_lock_status=0
if flock -n "$lockfd"; then
    lock_taken=1
    # Nobody was in the way, so whatever the sample caught had already released. Naming it
    # would credit the wait to a holder there was no wait for.
    lock_holder_seen=""
else
    # `-E "$LOCK_BUSY"` applies to the descriptor form as it did to the command form, and it
    # is what keeps CONTENTION distinct from any other flock failure: only a timeout returns
    # 75, and a genuine error still returns flock's own code, exactly as before. Dropping it
    # and treating every failure as busy would have reported "nothing was deployed, retry
    # shortly" for a lock file this wrapper could not even open.
    flock -w "$LOCK_WAIT" -E "$LOCK_BUSY" "$lockfd"
    flock_status=$?
    if [[ "$flock_status" == 0 ]]; then
        lock_taken=1
    fi
fi
lock_waited=$((SECONDS - lock_started))

if [[ "$lock_taken" == 1 ]]; then
    # Silent at 0s: an uncontended acquire is the ordinary case, and a line on every deploy
    # would bury the ones that mean something.
    if [[ "$lock_waited" -gt 0 ]]; then
        say_lock_acquired "$lock_waited" "$lock_holder_seen"
    fi
    # Snapshot HEAD, then hand the tree back. Everything after this point reads the snapshot,
    # so the tick, the rotate cron and every other session are free while this run deploys.
    snapshot_ok=0
    tags_ok=1
    reap_dead_snapshots
    if make_snapshot; then
        snapshot_ok=1
        # Still under the tree lock, for the reason enumerate_full_run_tags gives.
        if [[ ${#split_tags[@]} -eq 0 ]] && ! enumerate_full_run_tags; then
            tags_ok=0
            remove_snapshot
        fi
    fi
    flock -u "$lockfd"
    exec {lockfd}>&-
    if [[ "$snapshot_ok" == 0 ]]; then
        say_snapshot_failed
        exit "$SNAPSHOT_FAILED"
    fi
    if [[ "$tags_ok" == 0 ]]; then
        say_tag_enumeration_failed
        exit "$SNAPSHOT_FAILED"
    fi
    # From here the snapshot must go, whichever way this shell leaves.
    trap remove_snapshot EXIT
    take_service_locks
    service_lock_status=$?
    if [[ "$service_lock_status" == 0 ]]; then
        run_playbook_in_snapshot "$@"
        status=$?
    else
        status=$service_lock_status
    fi
    release_service_locks
    remove_snapshot
    trap - EXIT
else
    status=$flock_status
    exec {lockfd}>&-
fi

# After the lock is released and only on success. `--check` and `--dry-run` never reach here —
# both exec out well above — so a mode that changes nothing cannot annotate as though it had.
emit_deploy_annotation "$status"

if [[ "$lock_taken" != 1 && "$flock_status" != "$LOCK_BUSY" ]]; then
    say_lock_unavailable "$flock_status"
    exit "$LOCK_UNAVAILABLE"
fi

# A SERVICE lock, not the tree lock. Its own message: "could not take the tree lock" would send
# an operator to the tick and the rotate cron, and neither of those takes a service lock.
if [[ "$service_lock_status" != 0 ]]; then
    # take_service_lock already named the file it could not open; say nothing over it.
    if [[ "$service_lock_status" == "$LOCK_UNAVAILABLE" ]]; then
        exit "$LOCK_UNAVAILABLE"
    fi
    if [[ "$service_lock_status" != "$LOCK_BUSY" ]]; then
        say_lock_unavailable "$service_lock_status" "$LOCK_DIR"
        exit "$LOCK_UNAVAILABLE"
    fi
    echo "deploy: a service lock under $LOCK_DIR stayed busy for ${LOCK_WAIT}s -- nothing" >&2
    echo "  was deployed. Another deploy of one of these services is in progress:" >&2
    echo "  gitops-deploy.service, or another Claude session. Retry." >&2
    exit "$LOCK_BUSY"
fi

if [[ "$status" == "$LOCK_BUSY" ]]; then
    echo "deploy: could not take $LOCK after ${LOCK_WAIT}s -- nothing was deployed." >&2
    echo "  A deploy is already running. Likely holders: gitops-deploy.service" >&2
    echo "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron," >&2
    echo "  or another Claude session (uv run python scripts/dev/prune_worktrees.py)." >&2
    exit "$status"
fi

if [[ "$status" != 0 ]]; then
    # See PLAYBOOK_FAILED above: ansible's number is reported here, never returned, because
    # 2/3/4 mean something else to every consumer of this wrapper.
    echo "deploy: the playbook ran and failed (ansible-playbook exit $status) -- changes that" >&2
    echo "  applied before the failing task ARE live. Read the PLAY RECAP and the failing" >&2
    echo "  TASK above; this is not a tag, staleness or lock refusal." >&2
    exit "$PLAYBOOK_FAILED"
fi

exit 0
