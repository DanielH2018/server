#!/usr/bin/env python3
"""Generate the mechanical half of every k8s service role's `## At a glance` block, in place.

WHY. A role's CLAUDE.md opens with an "At a glance" block a session reads before touching the
service, and until 2026-09-19 every line of it was typed by hand: the deploy tag, the image,
the route, the claims, the auto-deploy stance. Each is declared once elsewhere — the
`containers_list` entry, `defaults/main.yml`, the templates — and a hand copy drifts the day
the source moves (#2058: homepage's doc pinned `:latest` while its defaults pinned a digest).
The only guard checked that the heading existed. This generator writes the fields that have a
single reader in the tree, and a pytest gate keeps the committed block equal to what it writes.

WHAT IT WRITES, and what it leaves alone. The block sits between two HTML-comment markers
directly under the heading; everything after the closing marker is hand-written and untouched.
It carries only facts with one source each: the deploy tag (`entry_tags`, the same precedence
`deploy.yml` uses), the image REPOSITORIES and the vars that pin them, the route and its auth
tier (the same derivation `docs/reference/services.md` prints, so the two cannot disagree),
every PVC claim by name, and the `k8s_autodeploy` stance with the role's own
`k8s_autodeploy_reason`. A judgement — why a claim is unbacked, what a route bypasses — stays
in the prose below the block.

THE IMAGE LINE NAMES THE REPOSITORY, NEVER THE TAG OR DIGEST. Renovate automerges a
non-major or digest bump to an eligible role's `_image:` pin (`renovate.json`, the
`k8s image` rules), and it edits `defaults/main.yml` alone. A generated line carrying the
resolved pin would fall stale on exactly those PRs, and the gate would turn every one of
them red — an automerge path traded for a doc field. The repository moves only in a
deliberate PR, where regenerating is one command. The tag is one `grep` away in the var
the line names.

NOT RUN BY THE DOCS-REFRESH CRON. That cron stages `docs/reference` and
`docs/assets/generated` only, and a generator writing under `ansible/roles/` would leave the
primary checkout dirty, which parks the GitOps deployer. Run it by hand after changing a
role's defaults, templates or `containers_list` entry, and commit the result in the same PR;
`scripts/docs/tests/test_gen_role_glance.py` fails CI until you do.

STATIC PARSING ONLY, like every generator here: role defaults through `yaml.safe_load`,
templates through the catalogue's regexes, nothing imported from the deployer.

Usage::

    uv run python scripts/docs/gen_role_glance.py          # write every stale block
    uv run python scripts/docs/gen_role_glance.py --check  # list stale docs, write nothing
"""

import argparse
import re
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
from lib.render_guard import ALL_VARS, containers_entries, entry_tags, load_yaml
from lib.repo_paths import REPO

SELF = "scripts/docs/gen_role_glance.py"
HEADING = "## At a glance"
# `generated_from:` is the literal the repo's other generated content carries; a reader
# who greps for it finds every generated region at once.
BEGIN = (
    f"<!-- generated_from: {SELF} -- do not edit between this line and the closing marker. "
    f"Regenerate with `uv run python {SELF}` after changing this role's defaults, "
    "templates or containers_list entry. -->"
)
END = "<!-- /generated_from -->"
WIDTH = 95
# The one host that declares k8s services (lib.k8s_roles.HOST_VARS says the same).
HOST_VARS = REPO / "ansible/inventory/host_vars/daniel-box.yml"

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]+$")
_JINJA_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class MissingHeading(ValueError):
    """The doc has no `## At a glance` heading to put the block under."""


# --- facts ---------------------------------------------------------------------------------


def image_repository(ref: str) -> str:
    """`lscr.io/linuxserver/sonarr:4.0.19@sha256:…` -> `lscr.io/linuxserver/sonarr`.

    A Jinja variable in the ref is kept as `<name>`: `{{ k8s_registry_pull_host }}/n8n:latest`
    is an in-cluster build, and `<k8s_registry_pull_host>/n8n` says so without pretending to
    know the host.
    """
    ref = _DIGEST_RE.sub("", ref.strip())
    ref = _JINJA_VAR_RE.sub(lambda m: f"<{m.group(1)}>", ref)
    head, sep, tail = ref.rpartition(":")
    # A `:` after the last `/` is a tag; before it, a registry port.
    if sep and "/" not in tail:
        return head
    return ref


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


def render_block(lines: list[str]) -> str:
    """The markers around the bullets, each bullet wrapped the way the prose beside it is."""
    wrapped = [
        textwrap.fill(
            line,
            width=WIDTH,
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        for line in lines
    ]
    return "\n".join([BEGIN, *wrapped, END]) + "\n"


# --- the in-place writer -------------------------------------------------------------------


def render_doc(text: str, block: str) -> str:
    """`text` with `block` as the first thing under the heading, replacing an older block.

    Only the region between the markers is generated. A doc that has the heading but no
    markers yet gets the block inserted directly under the heading, above whatever bullets
    were there — nothing hand-written is removed by this function, ever.
    """
    lines = text.split("\n")
    try:
        at = next(i for i, line in enumerate(lines) if line.rstrip() == HEADING)
    except StopIteration:
        raise MissingHeading(f"no `{HEADING}` heading") from None
    head = lines[: at + 1]
    rest = lines[at + 1 :]
    if rest and rest[0].startswith("<!-- generated_from:"):
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


def stale_docs(
    *,
    write: bool,
    host_vars: Path = HOST_VARS,
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> list[str]:
    """Role names whose committed block differs from a fresh render; writes them if asked.

    Raises `MissingHeading` naming the role for a deployed role whose doc has no heading.
    """
    group_vars = load_yaml(all_vars)
    stale: list[str] = []
    for entry in k8s_service_entries(host_vars):
        name = entry["name"]
        role_dir = k8s_roles / name
        doc = role_dir / "CLAUDE.md"
        text = doc.read_text() if doc.is_file() else ""
        block = render_block(
            glance_lines(
                entry,
                role_dir,
                group_vars=group_vars,
                k8s_roles=k8s_roles,
                all_vars=all_vars,
            )
        )
        try:
            fresh = render_doc(text, block)
        except MissingHeading as exc:
            raise MissingHeading(
                f"{doc.relative_to(REPO) if doc.is_relative_to(REPO) else doc}: {exc}"
            ) from None
        if fresh != text:
            stale.append(name)
            if write:
                doc.write_text(fresh)
    return stale


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
