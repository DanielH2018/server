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
from typing import NamedTuple

# Which role directory a changed path sits in, as ONE question asked once (#3048). Six regexes
# answered it before — `_ACTIVE_CONFIG`, `_ACTIVE_TASKS`, `_ACTIVE_META`, `_ACTIVE_ROLE`,
# `_ACTIVE_K8S` and `_SETUP_ROLE` — plus a copy in `narrow_broad.py` and a pair in
# `land_tags.py`, each re-deriving the same three path segments and differing only in which
# plane and which subdirectory it accepted.
#
# The third group takes a TRAILING SLASH, so a file at a role's own root reports no
# subdirectory rather than its own basename. The catch-all branch in
# `services_from_changed_paths` reads that field.
_ROLE_PATH = re.compile(r"^ansible/roles/(containers|k8s|setup)/([^/]+)/(?:([^/]+)/)?")


class RolePath(NamedTuple):
    """The role directory a changed path sits in.

    Attributes:
        plane: `containers`, `k8s` or `setup` — the tree, which decides what can apply it.
        role: the role directory's name. NOT necessarily a deploy tag: only a role with a
            `containers_list` entry has one, which is `land_tags.tag_for`'s question.
        subdir: the directory under the role the path sits in (`templates`, `tasks`, `meta`,
            `files`), or `""` for a file at the role's own root.
    """

    plane: str
    role: str
    subdir: str


def role_of(path: str) -> RolePath | None:
    """Which role directory `path` belongs to, or None when it belongs to none.

    The PLAIN mapper, deliberately: it answers the path shape and nothing else. Every policy
    about which roles and which files count stays with the caller, because the callers
    genuinely differ — this module routes `containers/common/` to the Pi before the role
    branches below are reached, `narrow_broad` reads a tree at a ref where
    `containers/archive/` no longer exists, and `land_tags.role_for` reads DIFF paths and so
    still excludes `common` and drops a `.md`. Folding one in would change what deploys.
    """
    m = _ROLE_PATH.match(path)
    return RolePath(m.group(1), m.group(2), m.group(3) or "") if m else None


# The Pi's shared Compose deploy path, which daniel-box's play never reads: Pi work
# (`ChangeSet.pi_shared`), not a broad change buying a full `deploy.yml` run here (#2805).
_PI_SHARED_PREFIX = "ansible/roles/containers/common/"

# Changes whose blast radius we don't try to scope automatically. Split by which manual playbook
# actually applies them, so the defer-and-alert can name the RIGHT one (2026-07-16 review M1):
# `deploy.yml` is a pure containers_list loop, so a setup-plane change deployed via deploy.yml is a
# silent no-op — it must be applied with `initial_setup.yml` instead.
_BROAD_DEPLOY_PREFIXES = (
    "ansible/templates/",  # shared macros (traefik/networks/resources/...)
    "ansible/inventory/",  # host_vars / group_vars
    "ansible/deploy.yml",
    # The task files deploy.yml imports. deploy.yml itself was already broad, but its three
    # sibling task dirs matched nothing: `role_of` is anchored to ansible/roles/, so a
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
# The two deploy-plane trees a per-path consumer rule exists for: a variable or a macro can be
# traced to the roles that read it. `narrow_broad.broad_path_tags` derives tags for these, and
# `probe_lib/releases.py` takes a staleness census over them.
_BROAD_CENSUS_PREFIXES = ("ansible/inventory/", "ansible/templates/")
# The rest of the deploy plane: paths whose content EVERY play reads, so no `--tags` value
# scopes a change to them.
#
# DERIVED rather than listed (#3048). `narrow_broad.PLAY_PREFIXES` restated the complement by
# hand, so a prefix added to `_BROAD_DEPLOY_PREFIXES` could go missing from it — and a
# deploy-plane path in neither half reads as narrowable by a rule that has none. Subtraction
# makes the two exhaustive by construction.
_BROAD_PLAY_PREFIXES = tuple(
    p for p in _BROAD_DEPLOY_PREFIXES if p not in _BROAD_CENSUS_PREFIXES
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


# The paths a comment-only edit may be read out of. Both broad-setup families, which is a
# superset of _BROAD_MANUAL_PREFIXES (all three bring-up playbooks are listed there too) --
# written as the union so widening either list widens this with it.
#
# The DEPLOY plane is deliberately absent. A comment-only edit under ansible/templates/ or
# ansible/inventory/ still runs a full deploy.yml, which costs twenty minutes and tells no
# lie; the failure this exists to stop is a verdict that sends an operator to a playbook with
# nothing to do, and that verdict only comes off the setup plane.
_COMMENT_ONLY_PREFIXES = tuple(
    dict.fromkeys(_BROAD_MANUAL_PREFIXES + _BROAD_SETUP_PREFIXES)
)
# `_content_lines` is a YAML reader: it knows block scalars, and `#` starts a comment. In a
# `.j2` a `#` line is usually rendered content, and in a `.sh` or `.py` the block-scalar rule
# is meaningless -- so anything else is UNCLASSIFIABLE and keeps its broad classification.
# Every _BROAD_MANUAL_PREFIXES path is a `.yml`, so this suffix guard changed nothing about
# the behaviour that shipped with PR #746.
_COMMENT_ONLY_SUFFIXES = (".yml", ".yaml")


def comment_only_broad_changes(paths, old_ref: str, new_ref: str, show) -> set[str]:
    """The broad-plane YAML paths whose change between two refs is comments only.

    The classifier decides by path alone, so a one-line comment edit to k3s-bringup.yml
    parked every session's landing until an operator ff-merged the primary checkout by hand
    (PR #746, 2026-09-02). A caller drops the paths returned here before classifying;
    the remaining paths still take the broad arm on their own.

    Eligibility widened from the bring-up playbooks to the whole setup plane on 2026-09-02.
    A comment-only edit to `roles/setup/k3s/defaults/main.yml` is not a manual path at all --
    it is an UNROUTABLE setup role, k3s living in k3s-bringup.yml rather than
    initial_setup.yml -- so PR #843 ended `needs-manual-apply`, naming a bring-up playbook
    with nothing to apply for three edited comments (issue #848). Reading the same question
    ("did any content change?") on both families keeps the deployer's defer-and-alert and
    land.sh's verdict from disagreeing about the same commit.

    `show(ref, path)` returns the file's text at that ref and raises when it cannot. A path
    that is added, deleted or unreadable on either side stays broad -- the fail-safe
    direction is to park, never to fast-forward past a change this did not read.
    """
    quiet: set[str] = set()
    for p in paths:
        if not p.endswith(_COMMENT_ONLY_SUFFIXES):
            continue
        if not any(p.startswith(prefix) for prefix in _COMMENT_ONLY_PREFIXES):
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
    # Which manual playbook a broad change needs — deploy.yml's plane (shared templates/inventory)
    # vs initial_setup.yml's (roles/setup/, requirements.yml, bring-up playbooks). `broad`
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
    pi_shared: bool = False  # a `_PI_SHARED_PREFIX` change: merged, never applied here
    # `tasks` is the defer-and-alert channel for a service's structural, not-auto-deployed dirs:
    # tasks/ plus the role-root catch-all (defaults/, vars/, handlers/, …), named for history.
    tasks: set[str] = field(default_factory=set)
    # k8s-platform role(s) that changed (ansible/roles/k8s/<role>/...) and are not promoted to
    # `k8s_deploy` below. Each defer-and-alerts; nothing this tick applies can cover one.
    k8s: set[str] = field(default_factory=set)
    # k8s service(s) whose change is an image-pin bump ELIGIBLE for auto-deploy, split out of
    # `k8s` by split_k8s_auto_deploy; `k8s` keeps its "defer-and-alert, never applied" meaning.
    k8s_deploy: set[str] = field(default_factory=set)
    # The newest commit in the range whose own diff reaches each `k8s` service, which
    # `k8s_unapplied` records instead of the tip (#3111). `deploy_phases.plan_tick` fills it.
    k8s_origins: dict[str, str] = field(default_factory=dict)
    # k8s roles that import a changed `files/*.py` owned by another role — see
    # shared_module_consumers. Kept separate from `k8s` so it stays inert for every consumer
    # that reads `k8s` directly; only k8s_remediation folds it in, and only after
    # intersecting with what this host declares.
    k8s_consumers: set[str] = field(default_factory=set)


def shared_module_consumers(paths, repo_root) -> set[str]:
    """k8s roles that import a changed `files/**/*.py` module owned by a DIFFERENT role.

    `role_of` maps a path to the role whose directory it sits in, which is right for a
    manifest and wrong for a shared library. `bridge/common.py` lives under monitor-bridge and
    is imported by autofix-bridge too, so the #407 five-module split made an edit there emit
    `--tags monitor-bridge` alone -- autofix-bridge's ConfigMap kept the old copy, and nothing
    reported it (2026-08-25 review M-2).

    Derived by reading the imports rather than listing the pair, because a hardcoded pair is
    the same guard-scope mistake one level up: it would go stale the first time a third role
    imported the module. Returns only the EXTRA roles; the owning role is already in `cs.k8s`.

    A module is identified by its dotted path under `files/`, so `files/bridge/common.py` is
    `bridge.common` and is matched in every spelling a consumer can use for it. The first
    version of this matched one level (`files/<name>.py`) and a bare `import bridge.common`, which
    reads as complete and is not: the moment the shared module moved into a package it would
    have stopped matching, and the deployer would have emitted `--tags monitor-bridge` alone
    again -- the exact silence this function exists to prevent, reintroduced by a rename.
    """
    from pathlib import Path

    k8s = Path(repo_root) / "ansible" / "roles" / "k8s"
    changed_modules = {
        _module_id(m.group(2)) for p in paths if (m := _FILES_MODULE_RE.match(p))
    }
    if not changed_modules or not k8s.is_dir():
        return set()

    owners = {m.group(1) for p in paths if (m := _FILES_MODULE_RE.match(p))}
    patterns = [_import_re(mod) for mod in changed_modules]
    consumers: set[str] = set()
    for role in k8s.iterdir():
        if not (role / "files").is_dir() or role.name in owners:
            continue
        for src in (role / "files").rglob("*.py"):
            if src.name.startswith("test_") or "__pycache__" in src.parts:
                continue
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            if any(pat.search(text) for pat in patterns):
                consumers.add(role.name)
                break
    return consumers


# A changed module under a k8s role's files/, at ANY depth: group 1 is the role, group 2 the
# path under files/. One level (`files/[^/]+\.py$`) was the original, and it is the shape
# repo-root CLAUDE.md's "a check that finds its own subject by pattern" rule is about.
_FILES_MODULE_RE = re.compile(
    r"^ansible/roles/k8s/([^/]+)/files/((?:[^/]+/)*[^/]+\.py)$"
)


def _module_id(rel_path: str) -> str:
    """`bridge/common.py` -> `bridge.common`; `bridge/common.py` -> `bridge.common`."""
    return rel_path[: -len(".py")].replace("/", ".")


def _import_re(module_id: str) -> re.Pattern:
    """Every import statement that binds `module_id`, as a consumer would spell it.

    For `bridge.common`: `import bridge.common`, `from bridge.common import x`, and
    `from bridge import common` (with or without `as`, anywhere in the imported list). For a
    flat `bridge.common` the last form has no package to come from, so only the first two.
    """
    dotted = re.escape(module_id)
    forms = [
        rf"import\s+{dotted}\b",
        rf"from\s+{dotted}\s+import\b",
    ]
    package, _, name = module_id.rpartition(".")
    if package:
        forms.append(
            rf"from\s+{re.escape(package)}\s+import\s+(?:[^\n]*[\s,(])?{re.escape(name)}\b"
        )
    return re.compile(r"^\s*(?:" + "|".join(forms) + ")", re.M)


# DECIDED: every `.md` in this repo is prose no playbook applies, with no carve-out for one
# under a role's `files/` or `templates/` directory (issue #2810). There were two answers before:
# this function's rule, and `scripts/deploy_tools/narrow_paths.is_prose`, which read a `.md`
# under those two directories as shippable because a task CAN copy or render one. Nothing does —
# the three that exist (`configarr/files/baseline/README.md` and two under
# `home-assistant/files/`) are named by no task, no template and no file list, because the roles
# that own them ship a NAMED list rather than a directory (issue #1715). So the carve-out cost a
# whole `ansible/deploy.yml` for a README nobody deploys, and bought nothing.
# `ansible/tests/deploy/test_no_role_ships_a_markdown_file.py` is what makes it safe to assert
# rather than derive: a task that starts shipping a `.md` fails that guard instead of silently
# landing a file the deployer skipped. `narrow_paths.is_prose` now calls this, so there is one
# definition; the reach across the role boundary is the one `narrow_broad` already makes.
def is_doc(path: str) -> bool:
    """Whether a changed path is documentation that reaches no host."""
    return path.endswith(".md")


def services_from_changed_paths(paths: list[str]) -> ChangeSet:
    """Classify one push's changed paths into a ChangeSet.

    Routes each path to the plane it belongs to — Docker service config, the tasks
    defer-and-alert channel, a k8s role, or one of the broad-change prefixes — in the order
    each branch below requires (test paths first, then documentation, then secrets, then
    broad-manual ahead of broad-setup, then the active-role regexes).

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
        # Documentation reaches no host either, and it is tested here — ONCE, ahead of every
        # plane branch — rather than on the arms that happen to notice it. The k8s arm and the
        # container catch-all carried their own `and not p.endswith(".md")`; the setup and
        # broad-deploy arms above them did not, so `roles/setup/<role>/CLAUDE.md` matched
        # _BROAD_SETUP_PREFIXES and routed prose to an `initial_setup.yml --tags <role>` apply
        # or a defer-and-alert, and `roles/containers/common/CLAUDE.md` matched what was then a
        # broad-deploy prefix and routed prose to a full `ansible/deploy.yml` (issue #1714).
        # _BROAD_MANUAL_PREFIXES is three exact `.yml` paths, so hoisting past it cannot change
        # what parks the tick.
        if is_doc(p):
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
            cs.setup_roles |= setup_roles_for(p)
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_SETUP_PREFIXES):
            cs.broad = True
            cs.broad_setup = True
            cs.setup_roles |= setup_roles_for(p)
            continue
        if p.startswith(_PI_SHARED_PREFIX):
            cs.pi_shared = True
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_DEPLOY_PREFIXES):
            cs.broad = True
            cs.broad_deploy = True
            continue
        at = role_of(p)
        if at is None:
            continue
        if at.plane == "k8s":
            # A k8s role change the deployer applies only as a promoted image-pin bump
            # (`split_k8s_auto_deploy`, `cs.k8s_deploy`); every other one defer-and-alerts.
            # The WHOLE role dir, not split by subdirectory the way containers/ is below,
            # because the alert only needs to name the role.
            cs.k8s.add(at.role)
        elif at.plane != "containers":
            # A setup-plane path, which `_BROAD_SETUP_PREFIXES` consumes before this loop
            # reaches here. Named rather than left to fall through, because the branches
            # below are about a CONTAINER role and `role_of` can hand them a setup path —
            # the `containers/`-anchored regexes it replaced could not.
            continue
        elif at.subdir in ("templates", "files"):
            # A bind-mounted file under a container role: the docker-compose.yml.j2 OR any
            # config template / files/ asset. It reaches the container on its next deploy, so
            # it maps to a scoped, health-gated redeploy rather than a silent ff-merge.
            cs.services.add(at.role)
        else:
            # tasks/, and the catch-all for any other file under a container role —
            # `defaults/`, `vars/`, `handlers/`, `meta/`, a file at the role's root, or a
            # future dir.
            # None is auto-deployed, and each changes what a deploy of that service does, so
            # it defer-and-alerts on the tasks channel instead of taking the silent docs-only
            # ff-merge — the same asymmetry the secrets / requirements.yml paths close. A
            # *.md never reaches here: the docs test at the top of the loop keeps it silent.
            cs.tasks.add(at.role)
    return cs


# A file one setup role installs from another's `files/`, mapped to the installing roles, so a
# change to it re-applies them beside the owner (#3306). Keyed by file, so no other module of the
# owner reaches them. `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree.
SETUP_FILES_SHIPPED_BY_OTHER_ROLES: dict[str, frozenset[str]] = {
    "ansible/roles/setup/gitops_deploy/files/gitops_markers.py": frozenset(
        {"deploy_ui", "renovate_agent"}
    ),
}


def setup_roles_for(path: str) -> set[str]:
    """Every setup role a change to `path` re-applies: its owner and each role shipping it."""
    at = role_of(path)
    if at is None or at.plane != "setup":
        return set()
    return {at.role} | SETUP_FILES_SHIPPED_BY_OTHER_ROLES.get(path, frozenset())


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
        for role in setup_roles_for(p):
            # A role initial_setup.yml does not include returns NOTHING: its tag would match
            # no task and exit 0, the guess this function's docstring forbids.
            if setup_role_playbook(role) != "ansible/initial_setup.yml":
                continue
            tags.add(setup_role_tag(role))
    return tags
