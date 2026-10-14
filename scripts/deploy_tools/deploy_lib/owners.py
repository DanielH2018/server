"""Which deploy tags are not Ansible's to deploy, and what `deploy.sh` does with each.

A k8s `containers_list` entry that carries `owner` (`lib.k8s_roles.tag_owners`) is applied by
Flux through `promote`, or is mid-handback, so an Ansible apply of its tag would be a second
writer. The plan is docs/target-architecture/deploy/d2-first-leaf.md, *What each entry point
does with a tag that is not Ansible-owned*. Each `deploy.sh` site asks this module:

- the front half refuses such a tag typed into `--tags`, with exit 5 (`refuse_flux_owned`);
- `deploy_tags.changed` and the shared-role expansion leave one out and name it
  (`drop_owned`, `owner_note`);
- `--list-services` names each on stderr before it runs `deploy_tags.py list`
  (`mark_owners`).
"""

import subprocess
import sys
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path

# Reach `lib.` and `deploy_tools.` from scripts/: an importer that did not bootstrap them
# itself (a test, a REPL) still resolves both.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.deploy_lib.flags import Refused
from deploy_tools.deploy_lib.messages import PROG, say_error
from lib.exit_codes import DEPLOY_FLUX_OWNED, FAILED
from lib.git import git
from lib.repo_paths import REPO


def flux_owned(repo_root: Path = REPO, at: str = "") -> dict[str, str]:
    """Each tag no Ansible path may deploy, mapped to its `owner`, as `tag_owners` reads it.

    Reads the k8s host's `containers_list` in `repo_root`'s working tree, or as commit `at`
    declares it. `deploy.sh --at <sha>` renders that commit, and a landing passes `--at` before
    the tick has fast-forwarded the checkout, so a tree read would miss the `owner` the commit
    being deployed has just set.

    Raises:
        ValueError: an entry's `owner` is outside `lib.k8s_roles.OWNER_VALUES`.
        subprocess.CalledProcessError: `at` does not hold the k8s host's host_vars file.
    """
    from lib.ansible_inventory import K8S_HOST_VARS
    from lib.k8s_roles import tag_owners

    rel = K8S_HOST_VARS.relative_to(REPO)
    if not at:
        return tag_owners(repo_root / rel)
    text = git("show", f"{at}:{rel.as_posix()}", cwd=repo_root).stdout
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / rel.name
        path.write_text(text)
        return tag_owners(path)


def owner_note(tag: str, owner: str) -> str:
    """Who applies `tag` instead of Ansible, as one clause every drop and refusal prints."""
    if owner == "flux":
        return f"`{tag}` is applied by Flux through `promote` (owner: flux)"
    return (
        f"`{tag}` is mid-handback (owner: {owner}): nothing applies it until a PR removes "
        "`owner`"
    )


def drop_owned(tags: Collection[str], repo_root: Path, site: str) -> set[str]:
    """`tags` less every tag `flux_owned(repo_root)` maps, with one stderr note per tag left out.

    Args:
        tags: the tags a site derived.
        repo_root: the checkout whose `containers_list` decides.
        site: the flag the note names, such as `--changed`.

    Raises:
        ValueError: as `flux_owned` does.
    """
    owners = flux_owned(repo_root)
    for tag in sorted(set(tags) & owners.keys()):
        print(
            f"{PROG}: {site}: left out {owner_note(tag, owners[tag])}.", file=sys.stderr
        )
    return set(tags) - owners.keys()


def mark_owners(owners: Mapping[str, str]) -> None:
    """Name each tag in `owners` on stderr, for `--list-services`.

    stderr rather than beside the tag in `deploy_tags.py list`, because that stdout is a
    contract: the locked half locks one service per line of it, and `deploy_ui` splits it on
    whitespace into the tags its API accepts. A mark there would join both lists as a tag.
    """
    for tag, owner in sorted(owners.items()):
        print(
            f"{PROG}: {owner_note(tag, owner)}; --tags {tag} exits 5.", file=sys.stderr
        )


def read_owners(plan) -> dict[str, str]:
    """`flux_owned` for `plan`'s checkout at its `--at` commit, read once per run.

    `plan` is `deploy_lib.run.Plan`; the map is cached on its `owners` field. Raises what
    `flux_owned` raises.
    """
    if plan.owners is None:
        plan.owners = flux_owned(plan.repo_root, plan.at_sha)
    return plan.owners


def refuse_flux_owned(plan) -> None:
    """Refuse with 5 when a tag in `plan.tags` is not Ansible's to deploy.

    The front half asks this after the staleness gate, for `validate_tags`' reason: a stale
    tree answers with stale ownership. `--skip-tag-check` does not skip it, because that flag
    bypasses a typo check and nothing else. `--check` and `--dry-run` ask it too: the play
    skips such an entry, so neither mode would show anything for it.

    An unreadable ownership map refuses with 1. Reading an unknown `owner` as Ansible-owned
    would hand the tag to both writers.

    Raises:
        Refused: with `DEPLOY_FLUX_OWNED`, or with `FAILED` when the map is unreadable.
    """
    if not plan.tags:
        return
    try:
        owners = read_owners(plan)
    except (ValueError, subprocess.CalledProcessError) as exc:
        say_error(
            "could not read which tags Flux applies -- nothing was deployed.",
            f"  {exc}",
        )
        raise Refused(FAILED) from None
    owned = [tag for tag in plan.tags if tag in owners]
    for tag in owned:
        say_error(
            f"{owner_note(tag, owners[tag])} -- nothing was deployed.",
            f"  `{tag}` lands through `promote` once its change merges, never through Ansible.",
        )
    if owned:
        raise Refused(DEPLOY_FLUX_OWNED)
