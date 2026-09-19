#!/usr/bin/env python3
"""Generate the mechanical half of every role's `## At a glance` block, in place.

WHY. A role's CLAUDE.md opens with an "At a glance" block a session reads before touching the
service, and until 2026-09-19 every line of it was typed by hand: the deploy tag, the image,
the route, the claims, the auto-deploy stance. Each is declared once elsewhere — the
`containers_list` entry, `defaults/main.yml`, the templates — and a hand copy drifts the day
the source moves (#2058: homepage's doc pinned `:latest` while its defaults pinned a digest).
The only guard checked that the heading existed. This generator writes the fields that have a
single reader in the tree, and a pytest gate keeps the committed block equal to what it writes.

WHAT IT WRITES, and what it leaves alone. The block sits between two HTML-comment markers
directly under the heading; everything after the closing marker is hand-written and untouched.
It carries only facts with one source each. A judgement — why a claim is unbacked, what a
route bypasses, why a pin is held back — stays in the prose below the block.

ONE FIELD SET PER ROLE SHAPE, not one schema (#2096). The three planes declare different
facts in different places, and a single schema would print "none" for most fields on most
roles:

- a k8s service role (`roles/k8s/<name>/`, every `platform: k8s` entry of the cluster host's
  `containers_list`): the deploy tag (`entry_tags`, the same precedence `deploy.yml` uses),
  the image REPOSITORIES and the vars that pin them, the route and its auth tier (the same
  derivation `docs/reference/services.md` prints, so the two cannot disagree), every PVC
  claim by name, and the `k8s_autodeploy` stance with the role's own `k8s_autodeploy_reason`;
- a setup role (`roles/setup/<name>/`, every directory but the include-only `common`): the
  playbook and tag that apply it, with the `when:` that restricts it to a host, read from the
  `roles:` lists and `include_role` tasks of the bring-up playbooks; every cron it installs,
  with its schedule; every systemd timer it ships, with its `[Timer]` cadence keys;
- a Pi compose role (`roles/containers/<name>/`, every entry of the Pi's `containers_list`):
  the deploy tag with the `-e target=daniel-pi` the Pi needs, the image repositories the
  compose template pins and the services that carry each, the entry's port, networks and
  Authelia stance, the `meta/deps.yml` ordering, and whether the role passes
  `common_config_changed` into `docker_deploy` (the wiring a bind-mounted config file needs
  to recreate the container — `roles/containers/common/CLAUDE.md`).

A doc with no `## At a glance` heading is refused on the k8s plane (all of them carry it,
and `test_k8s_roles_have_claude_md.py` requires it) and given one on the other two: the
setup docs never had the heading, so the generator inserts it before the doc's first
level-2 heading, or at the end of a doc that has none.

THE IMAGE LINE NAMES THE REPOSITORY, NEVER THE TAG OR DIGEST. Renovate automerges a
non-major or digest bump to an eligible role's `_image:` pin (`renovate.json`, the
`k8s image` rules), and it edits `defaults/main.yml` alone. A generated line carrying the
resolved pin would fall stale on exactly those PRs, and the gate would turn every one of
them red — an automerge path traded for a doc field. The repository moves only in a
deliberate PR, where regenerating is one command. The tag is one `grep` away in the var
the line names. The Pi's compose pins are moved by hand (Renovate's built-in compose
manager does not read a `.j2`), so the rule costs nothing there and is kept for one shape
of image line across every plane.

NOT RUN BY THE DOCS-REFRESH CRON. That cron stages `docs/reference` and
`docs/assets/generated` only, and a generator writing under `ansible/roles/` would leave the
primary checkout dirty, which parks the GitOps deployer. Run it by hand after changing a
role's defaults, templates, tasks, playbook entry or `containers_list` entry, and commit the
result in the same PR; `scripts/docs/tests/test_gen_role_glance.py` fails CI until you do.

STATIC PARSING ONLY, like every generator here: role defaults, tasks and playbooks through
`yaml.safe_load`, templates through regexes, nothing imported from the deployer. Jinja in a
schedule, a `when:` or a timer key is printed as written — an unresolved `{{ var }}` is the
honest rendering, the same rule `docs/reference/crons.md` follows.

Usage::

    uv run python scripts/docs/gen_role_glance.py          # write every stale block
    uv run python scripts/docs/gen_role_glance.py --check  # list stale docs, write nothing
"""

import argparse
import sys as _sys
import textwrap
from pathlib import Path as _Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path
from typing import Any

from catalog_backup import autodeploy_stance, claim_names
from catalog_facts import auth_tier, k8s_route
from catalog_model import K8S_ROLES
from glance_facts import (
    CONTAINERS_ROLES,
    PI_HOST_VARS,
    SETUP_ROLES,
    image_repository,
    pi_glance_lines,
    pi_service_entries,
    setup_glance_lines,
    setup_role_dirs,
)
from lib.render_guard import ALL_VARS, containers_entries, entry_tags, load_yaml
from lib.repo_paths import ANSIBLE, REPO

SELF = "scripts/docs/gen_role_glance.py"
HEADING = "## At a glance"
# `generated_from:` is the literal the repo's other generated content carries; a reader
# who greps for it finds every generated region at once. The marker names what moves the
# block, and that differs per role shape, so each plane opens with its own text.
BEGIN_PREFIX = f"<!-- generated_from: {SELF} -- "


def begin_marker(sources: str) -> str:
    return (
        f"{BEGIN_PREFIX}do not edit between this line and the closing marker. "
        f"Regenerate with `uv run python {SELF}` after changing this role's {sources}. -->"
    )


BEGIN = begin_marker("defaults, templates or containers_list entry")
BEGIN_SETUP = begin_marker("tasks, timer templates or playbook entry")
BEGIN_PI = begin_marker(
    "compose template, tasks, meta/deps.yml or containers_list entry"
)
END = "<!-- /generated_from -->"
WIDTH = 95
# The one host that declares k8s services (lib.k8s_roles.HOST_VARS says the same).
HOST_VARS = REPO / "ansible/inventory/host_vars/daniel-box.yml"


class MissingHeading(ValueError):
    """The doc has no `## At a glance` heading to put the block under."""


# --- k8s-plane facts (the setup and Pi readers are in glance_facts.py) ---------------------


def image_vars(
    role: str, defaults: dict[str, Any], group_vars: dict[str, Any]
) -> list[tuple[str, str]]:
    """`(var, ref)` for every `*_image` the role pins, in the role's defaults then group_vars.

    A pin hoisted to group_vars keeps the role's name as its prefix (`crowdsec_k8s_image`
    lives in all.yml because traefik's and authelia's sidecars read it too), which is how the
    hoisted one is found without a hand-kept list.
    """
    prefix = role.replace("-", "_") + "_"
    found = [
        (str(k), str(v))
        for k, v in defaults.items()
        if str(k).endswith("_image") and isinstance(v, str)
    ]
    found += [
        (str(k), str(v))
        for k, v in group_vars.items()
        if str(k).startswith(prefix)
        and str(k).endswith("_image")
        and isinstance(v, str)
    ]
    return found


def glance_lines(
    entry: dict[str, Any],
    role_dir: Path,
    *,
    group_vars: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> list[str]:
    """The block's bullet lines for one deployed k8s role, unwrapped."""
    name = entry["name"]
    defaults = load_yaml(role_dir / "defaults" / "main.yml")
    lines = [f'- **Deploy tag:** `--tags "{",".join(entry_tags(entry))}"`']

    images = image_vars(name, defaults, group_vars)
    if images:
        label = "Image" if len(images) == 1 else "Images"
        pins = ", ".join(f"`{image_repository(ref)}` (`{var}`)" for var, ref in images)
        lines.append(f"- **{label}:** {pins}")

    route = k8s_route(entry, k8s_roles, all_vars)
    if route.startswith("no route"):
        lines.append("- **Route:** none (no `templates/ingressroute.yaml.j2`)")
    else:
        route, lan_only = (
            route.removesuffix(" (LAN only)"),
            route.endswith(" (LAN only)"),
        )
        hosts = " · ".join(f"`{h.strip()}`" for h in route.split("·"))
        reach = " (LAN only)" if lan_only else ""
        # The catalogue's "none (public/no-auth)" reads wrong beside "(LAN only)".
        auth = auth_tier(entry).replace("none (public/no-auth)", "no Authelia")
        lines.append(f"- **Route:** {hosts}{reach}, {auth}")

    claims = claim_names(role_dir)
    if claims:
        # A claim named by a template loop variable (pihole's `{{ inst.claim }}`) is a real
        # claim the parser cannot name; say so rather than print the expression as a name.
        named = [c for c in claims if "{{" not in c]
        looped = [c for c in claims if "{{" in c]
        cells = ", ".join(f"`{claim}`" for claim in named)
        if looped:
            exprs = ", ".join(f"`{expr}`" for expr in looped)
            cells += (
                ", plus " if named else ""
            ) + f"the claims a template loop declares ({exprs})"
        label = "Claim" if len(claims) == 1 and not looped else "Claims"
        lines.append(f"- **{label}:** {cells}")
    else:
        lines.append("- **Claims:** none (no PVC)")

    stance, reason = autodeploy_stance(role_dir)
    if stance is None:
        lines.append(
            "- **Auto-deploy:** undeclared (no `k8s_autodeploy` in `defaults/main.yml`)"
        )
    elif stance:
        lines.append("- **Auto-deploy:** eligible (`k8s_autodeploy: true`)")
    else:
        lines.append(
            f"- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — {reason or 'no reason given'}"
        )
    return lines


def render_block(lines: list[str], begin: str = BEGIN) -> str:
    """The markers around the bullets, each bullet wrapped the way the prose beside it is."""
    wrapped = [
        textwrap.fill(
            line,
            width=WIDTH,
            # A sub-bullet hangs two spaces past its own indent, like the top-level ones.
            subsequent_indent=" " * (len(line) - len(line.lstrip(" ")) + 2),
            break_long_words=False,
            break_on_hyphens=False,
        )
        for line in lines
    ]
    return "\n".join([begin, *wrapped, END]) + "\n"


# --- the in-place writer -------------------------------------------------------------------


def _insert_heading(lines: list[str]) -> list[str]:
    """`lines` with the heading added where the k8s docs keep it: after the intro prose.

    That is before the doc's first level-2 heading. A doc with no level-2 heading gets it
    after the first paragraph under the title, and a doc with neither gets it appended.
    """
    try:
        at = next(i for i, line in enumerate(lines) if line.startswith("## "))
    except StopIteration:
        title = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
        if title is None:
            trailing = lines[-1:] == [""]
            return [
                *lines[: len(lines) - trailing],
                "",
                HEADING,
                "",
                *lines[len(lines) - trailing :],
            ]
        at = (
            next(
                (i for i in range(title + 2, len(lines)) if lines[i].strip() == ""),
                len(lines),
            )
            + 1
        )
        return [*lines[:at], HEADING, "", *lines[at:]]
    return [*lines[:at], HEADING, "", *lines[at:]]


def render_doc(text: str, block: str, *, create_heading: bool = False) -> str:
    """`text` with `block` as the first thing under the heading, replacing an older block.

    Only the region between the markers is generated. A doc that has the heading but no
    markers yet gets the block inserted directly under the heading, above whatever bullets
    were there — nothing hand-written is removed by this function, ever. A doc with no
    heading is refused unless `create_heading` asks for one (`_insert_heading` says where).
    """
    lines = text.split("\n")
    try:
        at = next(i for i, line in enumerate(lines) if line.rstrip() == HEADING)
    except StopIteration:
        if not create_heading:
            raise MissingHeading(f"no `{HEADING}` heading") from None
        lines = _insert_heading(lines)
        at = lines.index(HEADING)
    head = lines[: at + 1]
    rest = lines[at + 1 :]
    if rest and rest[0].startswith(BEGIN_PREFIX):
        try:
            close = next(i for i, line in enumerate(rest) if line.rstrip() == END)
        except StopIteration:
            raise ValueError(
                f"opening marker under `{HEADING}` has no `{END}`"
            ) from None
        rest = rest[close + 1 :]
    # One blank line between the block and the hand-written bullets, however the file had it.
    while rest and rest[0].strip() == "":
        rest.pop(0)
    return "\n".join(head) + "\n" + block + "\n" + "\n".join(rest)


def k8s_service_entries(host_vars: Path = HOST_VARS) -> list[dict[str, Any]]:
    """Every `platform: k8s` entry of the cluster host's `containers_list`."""
    return [e for e in containers_entries(host_vars) if e.get("platform") == "k8s"]


def _refresh(
    doc: Path, block: str, *, write: bool, create_heading: bool = False
) -> bool:
    """True when `doc`'s committed block differs from `block`; rewrites the doc if asked."""
    text = doc.read_text() if doc.is_file() else ""
    try:
        fresh = render_doc(text, block, create_heading=create_heading)
    except MissingHeading as exc:
        raise MissingHeading(
            f"{doc.relative_to(REPO) if doc.is_relative_to(REPO) else doc}: {exc}"
        ) from None
    if fresh == text:
        return False
    if write:
        doc.write_text(fresh)
    return True


def stale_k8s_docs(
    *,
    write: bool,
    host_vars: Path = HOST_VARS,
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> list[str]:
    """`k8s/<name>` for every deployed k8s role whose block differs from a fresh render.

    Raises `MissingHeading` naming the role for a deployed role whose doc has no heading.
    """
    group_vars = load_yaml(all_vars)
    stale: list[str] = []
    for entry in k8s_service_entries(host_vars):
        name = entry["name"]
        role_dir = k8s_roles / name
        block = render_block(
            glance_lines(
                entry,
                role_dir,
                group_vars=group_vars,
                k8s_roles=k8s_roles,
                all_vars=all_vars,
            )
        )
        if _refresh(role_dir / "CLAUDE.md", block, write=write):
            stale.append(f"k8s/{name}")
    return stale


def stale_setup_docs(
    *,
    write: bool,
    setup_roles: Path = SETUP_ROLES,
    playbooks_dir: Path = ANSIBLE,
) -> list[str]:
    """`setup/<name>` for every setup role whose block differs from a fresh render."""
    stale: list[str] = []
    for role_dir in setup_role_dirs(setup_roles):
        block = render_block(
            setup_glance_lines(role_dir, playbooks_dir=playbooks_dir), BEGIN_SETUP
        )
        if _refresh(role_dir / "CLAUDE.md", block, write=write, create_heading=True):
            stale.append(f"setup/{role_dir.name}")
    return stale


def stale_pi_docs(
    *,
    write: bool,
    pi_host_vars: Path = PI_HOST_VARS,
    containers_roles: Path = CONTAINERS_ROLES,
) -> list[str]:
    """`containers/<name>` for every Pi compose role whose block differs from a fresh render."""
    stale: list[str] = []
    for entry in pi_service_entries(pi_host_vars):
        name = entry["name"]
        role_dir = containers_roles / name
        block = render_block(pi_glance_lines(entry, role_dir), BEGIN_PI)
        if _refresh(role_dir / "CLAUDE.md", block, write=write, create_heading=True):
            stale.append(f"containers/{name}")
    return stale


def stale_docs(*, write: bool) -> list[str]:
    """Every plane's stale docs, `<plane>/<role>`; writes them if asked."""
    return (
        stale_k8s_docs(write=write)
        + stale_setup_docs(write=write)
        + stale_pi_docs(write=write)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 naming every stale doc; write nothing",
    )
    args = parser.parse_args(argv)
    stale = stale_docs(write=not args.check)
    if args.check:
        if stale:
            print(f"gen_role_glance: stale At a glance block in: {', '.join(stale)}")
            print(f"  regenerate with: uv run python {SELF}")
            return 1
        print("gen_role_glance: every At a glance block matches the tree")
        return 0
    print(f"gen_role_glance: {len(stale)} doc(s) written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
