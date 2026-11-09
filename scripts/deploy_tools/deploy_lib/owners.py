"""Which deploy tags no Ansible path deploys, and what `deploy.sh` does with each.

Flux applies every k8s `containers_list` entry through `promote`, so an Ansible apply of its
tag would be a second writer. The refusal reads an entry's platform, and the
`every-k8s-entry-is-flux-owned` census row holds that each such entry carries `owner: flux`.
Each `deploy.sh` site asks this module:

- the front half refuses such a tag typed into `--tags`, with exit 5 (`refuse_k8s_tags`);
- `--list-services` names each on stderr before it runs `deploy_tags.py list`
  (`mark_k8s_tags`).

`flux_owned` reads `owner` for the landing's gate.
"""

import subprocess
import sys
import tempfile
from collections.abc import Collection, Generator, Iterable
from contextlib import contextmanager
from pathlib import Path

# Reach `lib.` and `deploy_tools.` from scripts/: an importer that did not bootstrap them
# itself (a test, a REPL) still resolves both.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.deploy_lib.flags import Refused
from deploy_tools.deploy_lib.messages import PROG, say_error
from lib.ansible_inventory import K8S_HOST
from lib.exit_codes import DEPLOY_FLUX_OWNED, FAILED
from lib.git import git
from lib.repo_paths import REPO


@contextmanager
def _k8s_services(repo_root: Path, at: str) -> Generator[Path]:
    """Yield the k8s service list in `repo_root`'s tree, or a copy of it at commit `at`.

    A commit from before the list moved holds it in the k8s host's host_vars file, under
    the same `containers_list` key, so that file is read at such a commit.

    Raises:
        subprocess.CalledProcessError: `at` holds neither file.
    """
    from lib.ansible_inventory import K8S_HOST, host_vars_file
    from lib.repo_paths import K8S_SERVICES

    rel = K8S_SERVICES.relative_to(REPO)
    if not at:
        yield repo_root / rel
        return
    shown = git("show", f"{at}:{rel.as_posix()}", cwd=repo_root, check=False)
    if shown.returncode != 0:
        old = host_vars_file(K8S_HOST).relative_to(REPO).as_posix()
        shown = git("show", f"{at}:{old}", cwd=repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / rel.name
        path.write_text(shown.stdout)
        yield path


def flux_owned(repo_root: Path = REPO, at: str = "") -> dict[str, str]:
    """Each tag no Ansible path may deploy, mapped to its `owner`, as `tag_owners` reads it.

    Reads the k8s host's `containers_list` in `repo_root`'s working tree, or as commit `at`
    declares it. `deploy.sh --at <sha>` renders that commit, and a landing passes `--at` before
    the tick has fast-forwarded the checkout, so a tree read would miss the `owner` the commit
    being deployed has just set.

    Raises:
        ValueError: an entry's `owner` is outside `lib.k8s_roles.OWNER_VALUES`.
        subprocess.CalledProcessError: `at` holds no k8s service list.
    """
    from lib.k8s_roles import tag_owners

    with _k8s_services(repo_root, at) as path:
        return tag_owners(path)


def declared_k8s_tags(repo_root: Path = REPO, at: str = "") -> set[str]:
    """Every tag of a k8s `containers_list` entry, read from the file `flux_owned` reads.

    Raises:
        subprocess.CalledProcessError: `at` holds no k8s service list.
    """
    from lib.k8s_roles import k8s_tags

    with _k8s_services(repo_root, at) as path:
        return k8s_tags(path)


def run_target(args: Collection[str]) -> str:
    """The host `-e target=<host>` names in a run's passthrough `args`, or "" without one.

    Reads `-e target=X`, `-e=target=X`, `-etarget=X` and `--extra-vars` in both spellings; a
    later `target=` wins, as Ansible's own merge does. A `-e` value that is JSON or `@file`
    carries no plain `target=` token and reads as no target.
    """
    target = ""
    items = list(args)
    for i, arg in enumerate(items):
        if arg in ("-e", "--extra-vars"):
            value = items[i + 1] if i + 1 < len(items) else ""
        elif arg.startswith("--extra-vars="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-e") and not arg.startswith("--"):
            value = arg[2:].removeprefix("=")
        else:
            continue
        for pair in value.split():
            key, _, val = pair.partition("=")
            if key == "target":
                target = val
    return target


def k8s_note(tag: str) -> str:
    """Who applies `tag` instead of Ansible, as one clause every refusal and mark prints."""
    return f"`{tag}` is applied by Flux through `promote` (platform: k8s)"


def mark_k8s_tags(tags: Iterable[str]) -> None:
    """Name each of `tags` on stderr, for `--list-services`.

    stderr rather than beside the tag in `deploy_tags.py list`, because that stdout is a
    contract: the locked half locks one service per line of it. A mark there would read as a
    tag.
    """
    for tag in sorted(tags):
        print(f"{PROG}: {k8s_note(tag)}; --tags {tag} exits 5.", file=sys.stderr)


def read_k8s_tags(plan) -> set[str]:
    """`declared_k8s_tags` for `plan`'s checkout at its `--at` commit, read once per run.

    `plan` is `deploy_lib.run.Plan`; the set is cached on its `k8s_tags` field. Empty when
    `-e target=` names a host other than the k8s host. Raises what `declared_k8s_tags` raises.
    """
    if plan.k8s_tags is None:
        # Flux applies the k8s host's workloads alone. A run aimed at another host, such as
        # the Pi, deploys that host's own entry, so the k8s host's entry does not govern it
        # even when both hosts list the same tag (`wg-easy`, #5815).
        target = run_target(plan.args)
        if target and target != K8S_HOST:
            plan.k8s_tags = set()
        else:
            plan.k8s_tags = declared_k8s_tags(plan.repo_root, plan.at_sha)
    return plan.k8s_tags


def refuse_k8s_tags(plan) -> None:
    """Refuse with 5 when a tag in `plan.tags` names a k8s entry.

    The front half asks this after the staleness gate, for `validate_tags`' reason: a stale
    tree answers with a stale list. `--skip-tag-check` does not skip it, because that flag
    bypasses a typo check and nothing else. `--check` and `--dry-run` ask it too: the play
    skips such an entry, so neither mode would show anything for it.

    An unreadable service list refuses with 1. Reading it as declaring no k8s tag would
    hand the tag to both writers.

    Raises:
        Refused: with `DEPLOY_FLUX_OWNED`, or with `FAILED` when the list is unreadable.
    """
    if not plan.tags:
        return
    try:
        declared = read_k8s_tags(plan)
    except (ValueError, subprocess.CalledProcessError) as exc:
        say_error(
            "could not read which tags Flux applies -- nothing was deployed.",
            f"  {exc}",
        )
        raise Refused(FAILED) from None
    owned = [tag for tag in plan.tags if tag in declared]
    for tag in owned:
        say_error(
            f"{k8s_note(tag)} -- nothing was deployed.",
            f"  No Ansible path may apply `{tag}`; drop it from --tags to deploy the rest.",
        )
    if owned:
        raise Refused(DEPLOY_FLUX_OWNED)
