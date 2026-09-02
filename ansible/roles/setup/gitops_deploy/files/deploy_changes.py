# ansible/roles/setup/gitops_deploy/files/deploy_changes.py
"""Which services a pushed change reaches, and which plane it lands on.

`services_from_changed_paths` maps a git-diff file list to a `ChangeSet`: the active container
services to redeploy, the k8s roles touched, and the broad flags (shared template, inventory,
setup plane, bring-up playbook) that route a change away from a scoped deploy. The setup-role
routing (`setup_role_playbook`, `setup_role_tag`, `setup_tags_for`) lives here because it is
the same question asked of one path.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

# A bind-mounted file under an active container role's templates/ or files/ dir — the
# docker-compose.yml.j2 OR any config template / files/ asset (e.g. prometheus.yml.j2,
# authelia configuration.yml.j2, monitor-bridge/files/check.py). A change here only reaches the
# container on its next deploy, so it maps to a scoped, health-gated redeploy — closing the GitOps
# loop instead of a silent ff-merge. tasks/ and the role CLAUDE.md are deliberately NOT matched
# (structural / docs — deploy those manually). The negative lookahead excludes archive/<svc>/...
_ACTIVE_CONFIG = re.compile(
    r"^ansible/roles/containers/(?!archive/)([^/]+)/(?:templates|files)/"
)
# A change under an active container role's tasks/ dir. tasks/ is deliberately NOT auto-deployed
# (structural — deploy manually), but unlike a CLAUDE.md/doc edit it DOES change what a deploy would
# do, so a tasks-only push must be flagged (defer-and-alert), not silently ff-merged and left
# unapplied with no signal — the same asymmetry the secrets / requirements.yml paths already close.
# Same archive/ exclusion; common/tasks is caught earlier by the _BROAD_PREFIXES check.
_ACTIVE_TASKS = re.compile(r"^ansible/roles/containers/(?!archive/)([^/]+)/tasks/")
# A change under an active container role's meta/ dir (meta/deps.yml). meta/ is NOT auto-deployed
# (structural, like tasks/), but unlike a doc edit it DOES change what a deploy does:
# `ansible/filter_plugins/toposort.py` reads meta/deps.yml to build the cross-service deploy ORDER
# and the dep CLOSURE a scoped `--tags` deploy expands. So a meta-only push must be flagged
# (defer-and-alert), not silently ff-merged as an invisible graph change — the same asymmetry the
# tasks / secrets / requirements paths already close. (The toposort LOGIC in filter_plugins/ is
# already _BROAD_PREFIXES; this is its DATA.) Same archive/ exclusion; common/meta is caught earlier
# by the _BROAD_PREFIXES check.
_ACTIVE_META = re.compile(r"^ansible/roles/containers/(?!archive/)([^/]+)/meta/")
# Catch-all for ANY other non-doc file under an active container role — `defaults/`, `vars/`,
# `handlers/`, or a future dir. Like tasks/ these change what a deploy of that service does but
# aren't auto-deployed, so a change here must defer-and-alert (via the tasks channel) rather than
# fall through to the silent docs-only ff-merge. Checked LAST, so templates/files (deploy), tasks/,
# and meta/ have already claimed their paths; only the structural remainder reaches it. CLAUDE.md /
# *.md are docs and keep the silent path (the caller excludes them). Same archive/ exclusion.
_ACTIVE_ROLE = re.compile(r"^ansible/roles/containers/(?!archive/)([^/]+)/")
# A change under a k8s-platform role's dir (ansible/roles/k8s/<role>/...). This deployer only ever
# auto-deploys DOCKER-platform services (deploy(cs.services) runs the same --tags path _ACTIVE_CONFIG
# feeds), so unlike _ACTIVE_TASKS/_ACTIVE_META there is no "rode the scoped redeploy" case to
# subtract — a k8s role change is NEVER applied by this pipeline and must always defer-and-alert.
# Before this, every path under ansible/roles/k8s/** matched NONE of the regexes above (they're all
# containers/-scoped) and fell through to services_from_changed_paths returning an EMPTY ChangeSet,
# which main()'s `if not cs.services:` branch takes as a plain docs-only ff-merge — silent, on EVERY
# host with has_gitops (daniel-box, all 47 services platform: k8s). Matches the WHOLE role dir (not
# split into templates/tasks/meta like containers/) since a k8s role has no separate auto-deploy path
# for any of its subdirs to be scoped against — the alert just needs to name the role. *.md (role
# CLAUDE.md) stays a silent ff-merge, same as the containers/ catch-all.
_ACTIVE_K8S = re.compile(r"^ansible/roles/k8s/([^/]+)/")

# Build roles that render no workload of their own, mapped to the roles that run what they
# build. Deploying the key WITHOUT the value builds a new image that nothing rolls onto,
# and reports green doing it.
#
# WHY A COUPLING EXISTS AT ALL. image-builder appends a rebuilt image to `k8s_rebuilt_images`,
# which roles/k8s/manifests turns into `manifests_image_changed` and rolls on. That fact is
# PLAY-SCOPED, so the build and the rollout must happen in one ansible-playbook run; two runs
# lose it and leave the old pods up. `roles/k8s/n8n/defaults/main.yml` records the incident
# (2026-08-08 `@n8n/di`) and prescribes `--tags "n8n-images,n8n"`. That prescription was prose,
# so every path→tag derivation missed it: a Renovate Dockerfile bump derives `n8n-images` alone.
#
# ONE ENTRY, NOT A MECHANISM. Six roles under roles/k8s/ include image-builder, and five of
# them (code-server, homelab-mcp, ical-proxy, nut, pi-peer-backup) render their own workload
# manifest, so a single tag already covers build and roll and the fact never crosses a role
# boundary. n8n-images is the only split one. test_build_roll_couplings.py re-derives that
# population from the role sources, so a second split build role fails the test rather than
# silently inheriting the bug.
#
# ONE-DIRECTIONAL. Editing roles/k8s/n8n's manifests needs no rebuild — a manifest change rolls
# on its own — so this must not be read as a symmetric pair.
_BUILD_ROLL_COUPLINGS = {"n8n-images": ("n8n",)}


def expand_build_couplings(tags):
    """`tags` plus the workload roles any build role among them requires.

    Called by the TAG derivations -- `deploy_tags.py changed` and land_tags.py -- and
    deliberately NOT by `services_from_changed_paths`, whose `cs.k8s` also feeds
    `split_k8s_auto_deploy`. A coupled role is added with no changed path of its own, so
    `image_only()` would diff an untouched defaults/main.yml and could read the empty result
    as vacuously image-only, promoting a role nothing asked for. n8n is denylisted today, so
    that is a latent hazard rather than a live one -- which is exactly the kind that should
    not be created. Widening a hand-scoped deploy is safe: the added role is one a correct
    deploy would have included anyway, and deploying a workload whose image did not move
    rolls nothing.
    """
    widened = set(tags)
    for build_role, needs in _BUILD_ROLL_COUPLINGS.items():
        if build_role in widened:
            widened.update(needs)
    return widened


# Changes whose blast radius we don't try to scope automatically. Split by which manual playbook
# actually applies them, so the defer-and-alert can name the RIGHT one (2026-07-16 review M1):
# `deploy.yml` is a pure containers_list loop, so a setup-plane change deployed via deploy.yml is a
# silent no-op — it must be applied with `initial_setup.yml` instead.
_BROAD_DEPLOY_PREFIXES = (
    "ansible/templates/",  # shared macros (traefik/networks/resources/...)
    "ansible/inventory/",  # host_vars / group_vars
    "ansible/roles/containers/common/",  # shared deploy path
    "ansible/deploy.yml",
    # The task files deploy.yml imports. deploy.yml itself was already broad, but its three
    # sibling task dirs matched nothing: every _ACTIVE_* regex is anchored to ansible/roles/, so a
    # change to `pre_tasks/load_secrets.yml`, `tasks/k8s_batch.yml` (the k8s rollout-batch path) or
    # `post_tasks/k8s_stabilise_gate.yml` (the post-deploy stabilisation gate) returned a fully
    # EMPTY ChangeSet. main() has no catch-all branch — `if not cs.services:` ff-merges
    # unconditionally and both alert_deferred/alert_secrets_deferred no-op on empty fields — so
    # empty-because-unclassified was bit-for-bit indistinguishable from empty-because-docs: a
    # silent ff-merge, no alert, no deploy, on files that change what EVERY deploy does.
    # Deploy-plane rather than setup-plane because deploy.yml is the playbook this deployer runs;
    # `pre_tasks/load_secrets.yml` is also imported by initial_setup.yml, k3s-bringup.yml and
    # preflight.yml, but those only ever run by hand, so `ansible/deploy.yml` is the remediation
    # that actually applies the change on this host. Proportionate: these three dirs changed in 7
    # commits in the repo's whole history, so defer-and-alert here will rarely fire.
    "ansible/pre_tasks/",
    "ansible/tasks/",
    "ansible/post_tasks/",
    "ansible/filter_plugins/",  # toposort
    # ansible.cfg is a repo-root file read fresh by every ansible-playbook the deployer runs
    # (WorkingDirectory is the repo root, so ./ansible.cfg applies) but maps to no service — it sets
    # inventory/roles_path/collections_path/fact-caching, so a bad value mis-attributes a later
    # unrelated deploy's failure (2026-07-15 review M1). It changes rarely and operator-driven, so
    # broad (defer-and-alert) fits. pyproject.toml + uv.lock are deliberately NOT broad: they churn on
    # a predictable schedule (renovate.json lockFileMaintenance, daily + every dep-pin bump re-resolves
    # uv.lock), and the broad path never ff-merges — it parks local behind origin, and since broad is
    # checked before services, every later image bump (incl. CVE automerges) then piles up unapplied
    # behind the stuck lockfile until a manual full deploy (2026-07-15 review H1). A bad lockfile is
    # already caught pre-merge by CI `uv lock --check` and at deploy by the health-gate rollback, so
    # letting them take the silent ff-merge path (pre-2026-07-15 behavior) is the safer trade.
    "ansible.cfg",
)
# Broad changes applied by initial_setup.yml, NOT deploy.yml — deploy.yml renders NOTHING for these,
# so the defer-alert must point the operator at `initial_setup.yml --tags <role>`. Naming deploy.yml
# here is a no-op that leaves the change unapplied while a plain `git merge --ff-only` clears the
# divergence — worst case a fix to gitops_deploy.py itself ff-merges and the host keeps running the
# OLD code forever, with last_run still updating (old code writes it) so no monitor catches it.
_BROAD_SETUP_PREFIXES = (
    # Galaxy collections: installed by sops_setup — `initial_setup.yml --tags collections`.
    "ansible/requirements.yml",
    # Setup roles (gitops_deploy itself, renovate_notify, sops_setup, …): `--tags <role>`.
    "ansible/roles/setup/",
    # The bring-up playbooks — they only run by hand.
    "ansible/initial_setup.yml",
    "ansible/bootstrap.yml",
    "ansible/k3s-bringup.yml",
)
# Broad paths the deployer must NEVER apply itself, even though every other broad path now
# fast-forwards and applies.
#
# The bring-up playbooks are hand-run by construction (see broad_remediation), and
# initial_setup.yml unqualified is a whole-host reprovision rather than a scoped apply.
#
# These keep the OLD behaviour in full: defer, alert, and do not fast-forward. Staying
# parked is what keeps `behind_since` set, and that marker is the only durable signal that
# an unapplied plane exists.
#
# DECIDED: roles/setup/gitops_deploy/ — the deployer's own role — is NOT here, and applies
# itself like any other setup role. It sat here until 2026-09-01 on the claim that applying
# it "runs a playbook whose handler restarts the unit executing the tick", so the run would be
# SIGTERMed partway. The handler does not restart anything: `Run gitops-deploy once` is
# `ansible.builtin.systemd: state: started`, and Ansible's systemd module treats an
# `activating` unit as already running (`is_running_service` accepts `active` and
# `activating`), so from inside a tick it is a no-op. The only other handler is a
# daemon-reload, which a running oneshot unit survives. What the park DID do, three times on
# 2026-09-01 alone (#707, #712, #714): stop every other session's landing until an
# operator hand-ran `initial_setup.yml --tags gitops_deploy` in the primary checkout and
# ff-merged, because `deploy.sh` refuses a tree behind origin and the tick would not move it.
# A self-apply that fails takes the broad-apply failure path — hold_sha, hold_plane, alert —
# which is the same containment every other setup role gets, and the code it installs has
# passed master CI, which is the same gate every other role gets. The unit's own state
# (`config.env`, `/opt/gitops-deploy/*.py`) is read at the START of a tick and a mid-tick
# overwrite reaches only the next one, which is the tick that should run the new code anyway.
_BROAD_MANUAL_PREFIXES = (
    "ansible/bootstrap.yml",
    "ansible/k3s-bringup.yml",
    "ansible/initial_setup.yml",
)
# The SOPS-encrypted secrets file. A change here maps to no service template, but the new
# value only reaches a container on its next deploy — so a secrets-ONLY push must NOT be
# silently fast-forwarded; the deployer defers-and-alerts (see gitops_deploy.py). NOT in
# _BROAD_PREFIXES on purpose: the /add-secret flow ships secrets.yml WITH the consuming
# template, and that should stay a scoped single-service deploy, not a manual full deploy.
_SECRETS_FILE = "ansible/vars/secrets.yml"

# `key: |` / `key: >-` and their chomping/indent modifiers, with or without a trailing comment.
_BLOCK_SCALAR_HEAD = re.compile(r":\s*[|>][-+0-9]*\s*(?:#.*)?$")


def _content_lines(text: str) -> list[str]:
    """The lines of a YAML file that carry meaning.

    Full-line comments and blank lines are dropped, except inside a block scalar, where a
    line starting with `#` is part of the string and a blank line is a newline in it. The
    scalar runs from its `key: |` line until the next non-blank line indented no deeper
    than that key. Trailing comments on a content line are kept as-is: telling `# note`
    from a `#` inside a quoted value needs a parser, and the deployer runs stdlib-only
    (`uv run --no-project`), so the safe reading is that such a line changed.
    """
    kept: list[str] = []
    scalar_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if scalar_indent is not None:
            if stripped and indent <= scalar_indent:
                scalar_indent = None
            else:
                kept.append(line.rstrip())
                continue
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(line.rstrip())
        if _BLOCK_SCALAR_HEAD.search(line):
            scalar_indent = indent
    return kept


def comment_only_manual_changes(paths, old_ref: str, new_ref: str, show) -> set[str]:
    """The _BROAD_MANUAL_PREFIXES paths whose change between two refs is comments only.

    The classifier decides by path alone, so a one-line comment edit to k3s-bringup.yml
    parked every session's landing until an operator ff-merged the primary checkout by hand
    (PR #746, 2026-09-02). A caller drops the paths returned here before classifying;
    the remaining paths still take the manual arm on their own.

    `show(ref, path)` returns the file's text at that ref and raises when it cannot. A path
    that is added, deleted or unreadable on either side stays broad -- the fail-safe
    direction is to park, never to fast-forward past a change this did not read.
    """
    quiet: set[str] = set()
    for p in paths:
        if not any(p.startswith(prefix) for prefix in _BROAD_MANUAL_PREFIXES):
            continue
        try:
            before = show(old_ref, p)
            after = show(new_ref, p)
        except RuntimeError, subprocess.CalledProcessError, OSError:
            continue
        if _content_lines(before) == _content_lines(after):
            quiet.add(p)
    return quiet


def _is_test_only_path(path: str) -> bool:
    """Whether a changed path is test-suite material that no role ever ships to a host.

    Two shapes. The directory shape is the one the tree uses: `ansible/tests/` holds the
    repo-wide guards plus `_helpers.py`, which matches no name pattern at all, and every other
    suite sits in a role-local or per-script-directory `tests/` — a layout
    `ansible/tests/repo/test_testpaths_covers_every_test_file.py` enforces. The name shape,
    a `test_*.py` or `conftest.py` wherever it sits, covers the one deliberate exception,
    `scripts/conftest.py`, and a test a session adds beside code before that guard catches it.

    The invariant this rests on: nothing under `ansible/` copies a test file to a host.
    `ansible/tests/repo/test_no_role_ships_a_test_file.py` is the tree-wide guard, so a role that
    starts shipping one fails there rather than silently widening this predicate into a hole.
    """
    if path.startswith("ansible/tests/"):
        return True
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


@dataclass
class ChangeSet:
    """What one push's changed paths add up to: which services to deploy, which planes are broad.

    Each field's own comment documents its meaning and how it interacts with the others.
    """

    services: set[str] = field(default_factory=set)
    broad: bool = False
    # Which manual playbook a broad change needs — deploy.yml's plane (shared templates/inventory/
    # common) vs initial_setup.yml's (roles/setup/, requirements.yml, bring-up playbooks). `broad`
    # stays the OR so the existing defer branch is unchanged; these drive the alert's remediation
    # command so a setup-plane change isn't sent to deploy.yml (a no-op). A push can set both.
    broad_deploy: bool = False
    broad_setup: bool = False
    # The `roles/setup/<name>` dirs this push touched, so the remediation can name the
    # playbook that actually includes each one rather than assuming initial_setup.yml.
    setup_roles: set[str] = field(default_factory=set)
    # A broad path the deployer must not apply itself (_BROAD_MANUAL_PREFIXES). ORed across
    # the push: ONE manual path makes the whole tick manual, because a half-applied broad
    # change is exactly the state the defer-and-alert arm exists to prevent.
    broad_manual: bool = False
    secrets: bool = False
    # `tasks` is the defer-and-alert channel for a service's structural, not-auto-deployed dirs:
    # tasks/ plus the _ACTIVE_ROLE catch-all (defaults/, vars/, handlers/, …). The alert names all
    # of them, so the field keeps its name for continuity even though it's no longer tasks/-only.
    tasks: set[str] = field(default_factory=set)
    meta: set[str] = field(default_factory=set)
    # k8s-platform role(s) that changed (ansible/roles/k8s/<role>/...). Distinct from `tasks`/`meta`:
    # this deployer has no mechanism that EVER applies a k8s role change (deploy(cs.services) only
    # ever tags Docker-platform roles matched by _ACTIVE_CONFIG), so it always defer-and-alerts —
    # there's no "rode a scoped redeploy of the same service" case to subtract deployed against.
    k8s: set[str] = field(default_factory=set)
    # k8s service(s) whose change is an image-pin bump ELIGIBLE for auto-deploy, split out of
    # `k8s` by split_k8s_auto_deploy. `k8s` keeps its "defer-and-alert, never applied" meaning,
    # so every existing consumer of that field is unchanged and this stays inert until a service
    # actually qualifies.
    k8s_deploy: set[str] = field(default_factory=set)
    # k8s roles that import a changed `files/*.py` owned by another role — see
    # shared_module_consumers. Kept separate from `k8s` so it stays inert for every consumer
    # that reads `k8s` directly; only k8s_remediation folds it in, and only after
    # intersecting with what this host declares.
    k8s_consumers: set[str] = field(default_factory=set)


def shared_module_consumers(paths, repo_root) -> set[str]:
    """k8s roles that import a changed `files/*.py` module owned by a DIFFERENT role.

    `_ACTIVE_K8S` maps a path to the role whose directory it sits in, which is right for a
    manifest and wrong for a shared library. `bridge_common.py` lives under monitor-bridge and
    is imported by autofix-bridge too, so the #407 five-module split made an edit there emit
    `--tags monitor-bridge` alone -- autofix-bridge's ConfigMap kept the old copy, and nothing
    reported it (2026-08-25 review M-2).

    Derived by reading the imports rather than listing the pair, because a hardcoded pair is
    the same guard-scope mistake one level up: it would go stale the first time a third role
    imported the module. Returns only the EXTRA roles; the owning role is already in `cs.k8s`.
    """
    from pathlib import Path

    k8s = Path(repo_root) / "ansible" / "roles" / "k8s"
    changed_modules = {
        Path(p).stem
        for p in paths
        if re.match(r"^ansible/roles/k8s/[^/]+/files/[^/]+\.py$", p)
    }
    if not changed_modules or not k8s.is_dir():
        return set()

    owners = {
        m.group(1)
        for p in paths
        if (m := re.match(r"^ansible/roles/k8s/([^/]+)/files/[^/]+\.py$", p))
    }
    consumers: set[str] = set()
    for role in k8s.iterdir():
        if not (role / "files").is_dir() or role.name in owners:
            continue
        for src in (role / "files").glob("*.py"):
            if src.name.startswith("test_"):
                continue
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            if any(
                re.search(r"^\s*(?:import|from)\s+%s\b" % re.escape(mod), text, re.M)
                for mod in changed_modules
            ):
                consumers.add(role.name)
                break
    return consumers


def services_from_changed_paths(paths: list[str]) -> ChangeSet:
    """Classify one push's changed paths into a ChangeSet.

    Routes each path to the plane it belongs to — Docker service config, the tasks/meta
    defer-and-alert channels, a k8s role, or one of the broad-change prefixes — in the order
    each branch below requires (test paths first, then secrets, then broad-manual ahead of
    broad-setup, then the active-role regexes).

    Args:
        paths: repo-relative paths changed between local and origin.

    Returns:
        A ChangeSet describing what this push reaches.
    """
    cs = ChangeSet()
    for p in paths:
        # Test-suite files reach no host, so they must not drive a deploy decision. Checked
        # FIRST, before every prefix below, because the prefixes match on path alone: a test
        # under roles/setup/gitops_deploy/ read as broad_manual and parked the whole tick,
        # and one under roles/k8s/<svc>/files/ read as a k8s change and defer-alerted. PR #707
        # was three test files and did both, costing two hand-run commands to clear (2026-09-01).
        #
        # An empty ChangeSet is the right outcome, not a hole: gitops_deploy.py takes the
        # `if not cs.services` branch and fast-forwards, exactly as it does for a docs-only push.
        if _is_test_only_path(p):
            continue
        if p == _SECRETS_FILE:
            cs.secrets = True
            continue
        # Tested FIRST, and it does not `continue` past the plane flags below: the manual
        # set overlaps _BROAD_SETUP_PREFIXES (the bring-up playbooks sit in both), and the
        # alert still needs to name the right remediation playbook.
        if any(p.startswith(prefix) for prefix in _BROAD_MANUAL_PREFIXES):
            cs.broad = True
            cs.broad_manual = True
            cs.broad_setup = True
            _note_setup_role(cs, p)
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_SETUP_PREFIXES):
            cs.broad = True
            cs.broad_setup = True
            _note_setup_role(cs, p)
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_DEPLOY_PREFIXES):
            cs.broad = True
            cs.broad_deploy = True
            continue
        m = _ACTIVE_CONFIG.match(p)
        if m:
            cs.services.add(m.group(1))
            continue
        t = _ACTIVE_TASKS.match(p)
        if t:
            cs.tasks.add(t.group(1))
            continue
        mt = _ACTIVE_META.match(p)
        if mt:
            cs.meta.add(mt.group(1))
            continue
        k = _ACTIVE_K8S.match(p)
        if k and not p.endswith(".md"):
            cs.k8s.add(k.group(1))
            continue
        # Catch-all: any other non-doc file under an active container role (defaults/, vars/,
        # handlers/, …). Not auto-deployed but it changes what a deploy does — defer-and-alert
        # via the tasks channel instead of a silent ff-merge. *.md (CLAUDE.md, README) are docs
        # and keep the silent path.
        r = _ACTIVE_ROLE.match(p)
        if r and not p.endswith(".md"):
            cs.tasks.add(r.group(1))
    return cs


_SETUP_ROLE = re.compile(r"^ansible/roles/setup/([^/]+)/")


def _note_setup_role(cs: "ChangeSet", path: str) -> None:
    """Record which setup role a broad path belongs to, if any. A bring-up playbook has none."""
    m = _SETUP_ROLE.match(path)
    if m:
        cs.setup_roles.add(m.group(1))


# Setup roles `ansible/initial_setup.yml` does NOT include, mapped to the playbook that does.
# `None` means no playbook includes the role at all.
#
# THE BUG THIS EXISTS TO KILL. Both functions below used to assume every directory under
# `roles/setup/` was a tag in initial_setup.yml. It is not, and the failure is silent in the
# worst way: `--tags` matching nothing makes Ansible exit 0, so the deployer ff-merges, runs a
# playbook that does nothing, and records a successful apply. `setup_tags_for`'s own docstring
# names that outcome as the reason it returns an empty set rather than a guess — it was
# guessing anyway.
#
# Occurred 2026-09-01 with PR #702, a `roles/setup/k3s/` change installing a host DNS
# forwarder. The role appears only in `k3s-bringup.yml`, so the tick's `initial_setup.yml
# --tags k3s` matched no task; the forwarder had to be installed by hand afterwards, and
# nothing in the pipeline said it had not been.
#
# `common` is the sharper shape: no playbook includes it, and it is not dead code — two roles
# read its templates by absolute path, on two different hosts. A change to its shared
# resolv.conf.j2 has to be applied twice, via k3s-bringup.yml on daniel-box and via
# initial_setup.yml on daniel-pi, and neither is what the old code named.
_SETUP_ROLES_OUTSIDE_INITIAL_SETUP: dict[str, str | None] = {
    "k3s": "ansible/k3s-bringup.yml",
    "common": None,
}
# Setup roles whose `--tags` value is not their directory name. Same silent-exit-0 failure:
# `--tags chezmoi_setup` matches nothing, because the playbook tags that role `chezmoi`.
_SETUP_ROLE_TAG_OVERRIDES = {"chezmoi_setup": "chezmoi"}


def setup_role_playbook(role: str) -> str | None:
    """The playbook that applies a setup role, or None when no playbook includes it."""
    if role in _SETUP_ROLES_OUTSIDE_INITIAL_SETUP:
        return _SETUP_ROLES_OUTSIDE_INITIAL_SETUP[role]
    return "ansible/initial_setup.yml"


def setup_role_tag(role: str) -> str:
    """The `--tags` value that actually selects a setup role, which is not always its name."""
    return _SETUP_ROLE_TAG_OVERRIDES.get(role, role)


def setup_tags_for(paths) -> set[str]:
    """The `initial_setup.yml --tags` values a set of setup-plane paths needs.

    broad_remediation emits a literal `<role>` placeholder, which is fine for a human
    reading an alert and useless for a machine about to run the playbook. This derives the
    real tags, and the alert text uses it too so the two can never disagree.

    Returns an EMPTY set for anything it cannot resolve — a bring-up playbook, or any path
    in _BROAD_MANUAL_PREFIXES. Empty means "cannot be applied automatically", which the
    caller must treat as a deferral rather than as an unscoped run. Returning a wrong tag
    would be worse than returning none: `--tags` matching nothing makes Ansible exit 0, so
    the deployer would report a successful apply having changed nothing at all.
    """
    tags: set[str] = set()
    for p in paths:
        if any(p.startswith(prefix) for prefix in _BROAD_MANUAL_PREFIXES):
            continue
        if p == "ansible/requirements.yml":
            # Installed by sops_setup — see the comment on _BROAD_SETUP_PREFIXES.
            tags.add("collections")
            continue
        m = _SETUP_ROLE.match(p)
        if m:
            role = m.group(1)
            # A role initial_setup.yml does not include cannot be applied by the automatic
            # arm at all, so it must return NOTHING and route to defer-and-alert. Returning
            # the role name here is the guess this function's docstring forbids: the run
            # would exit 0 having matched no task.
            if setup_role_playbook(role) != "ansible/initial_setup.yml":
                continue
            tags.add(setup_role_tag(role))
    return tags
