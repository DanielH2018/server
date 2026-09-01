"""Tree-wide guard: a value-producing task must not carry both `config` and `deploy`.

WHY THIS EXISTS. Tags union for SELECTION and intersect for EXCLUSION. `--tags config` picks up
a `[config, deploy]` task and so does `--tags deploy`, which reads as "covered either way" — but
`--skip-tags deploy` drops it, because exclusion matches ANY tag. The repo's documented
config-only form is `--tags <svc> --skip-tags deploy`, so the dual tag breaks exactly the
invocation it looks like it protects.

Bit on 2026-08-16 adding `k8s_dry_run`: `manifests_dest_dir` was set by a `set_fact` read by the
config-tagged renders AND the deploy-tagged apply. Tagged `[config, deploy]` it passed
`--tags freshrss`, `--tags freshrss --dry-run` and `--check`, then failed
`--tags freshrss --skip-tags deploy` with `'manifests_dest_dir' is undefined` — ok=61 failed=1,
on a path that worked before the branch. The fix is `tags: [always]`, which no `--skip-tags`
removes.

WHY IT IS NARROWED TO PRODUCERS. A dual tag on a plain action is a selection choice someone may
genuinely want, and `--skip-tags deploy` skipping it may be exactly right. The hazard is specific
to a task whose OUTPUT is read on the other side of the split: `register:` or `set_fact:`. Those
have no legitimate dual-tagged form, because the consumer's run and the producer's run come apart
under a single `--skip-tags`. `test_a_dual_tagged_plain_action_is_not_flagged` pins that scope, so
a later widening has to be deliberate.

WHAT THE REAL-TREE ASSERTION IS AND IS NOT WORTH. The tree holds ZERO violations today, so
`test_no_value_producer_carries_both_tags` passing is not evidence the rule works — a rule that
matched nothing at all would pass it identically. The synthetic pairs below are the only proof
this can go red. That is the whole reason they exist.

Scope: `roles/**/tasks/*.yml`, the same surface as `test_conditional_register_consumers.py`.
Playbook-level `pre_tasks`/`post_tasks` are not walked.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from _helpers import REPO as _REPO_ROOT
from _helpers import ROLES as _ROLES
from _helpers import walk_tasks

_SPLIT_TAGS = frozenset({"config", "deploy"})

# The two ways a task hands a value to a later one. A dual tag on either is the documented bug.
_PRODUCER_KEYS = ("register", "set_fact", "ansible.builtin.set_fact")


def _task_files() -> list[Path]:
    return sorted(p for p in _ROLES.rglob("tasks/*.yml") if "archive" not in p.parts)


def _tags_of(task: dict) -> set[str]:
    tags = task.get("tags")
    if isinstance(tags, str):
        return {tags}
    if isinstance(tags, list):
        return {str(t) for t in tags}
    return set()


def _produces(task: dict) -> bool:
    return any(key in task for key in _PRODUCER_KEYS)


def dual_tagged_producers(tasks) -> list[str]:
    """Names of tasks that produce a value and carry both split tags.

    `walk_tasks` includes `block:` wrappers on purpose — a block carries the tags that govern
    its children, so a dual tag written on the wrapper is the same defect one level up.
    """
    hits = []
    for task in walk_tasks(tasks):
        if _produces(task) and _SPLIT_TAGS <= _tags_of(task):
            hits.append(str(task.get("name", "<unnamed>")))
    return hits


def test_no_value_producer_carries_both_tags() -> None:
    """The real tree. See the module docstring on why this passing proves little on its own."""
    offenders = []
    for path in _task_files():
        try:
            doc = yaml.safe_load(path.read_text())
        except (
            yaml.YAMLError
        ):  # a template-bearing tasks file is another guard's problem
            continue
        if not isinstance(doc, list):
            continue
        for name in dual_tagged_producers(doc):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}: {name}")

    assert not offenders, (
        "these tasks register or set a fact while tagged both `config` and `deploy`, so "
        "`--skip-tags deploy` drops them and a config-tagged consumer reads an undefined "
        "variable. Use `tags: [always]`, which no --skip-tags removes:\n  "
        + "\n  ".join(offenders)
    )


def test_a_dual_tagged_set_fact_is_flagged() -> None:
    """The 2026-08-16 shape, verbatim."""
    doc = yaml.safe_load(
        """
        - name: Resolve the manifest destination
          ansible.builtin.set_fact:
            manifests_dest_dir: /etc/rancher/k3s/manifests
          tags: [config, deploy]
        """
    )
    assert dual_tagged_producers(doc) == ["Resolve the manifest destination"]


def test_a_dual_tagged_register_is_flagged() -> None:
    doc = yaml.safe_load(
        """
        - name: Read the deploy commit
          ansible.builtin.command: git rev-parse HEAD
          register: deploy_commit
          tags: [config, deploy]
        """
    )
    assert dual_tagged_producers(doc) == ["Read the deploy commit"]


def test_a_dual_tag_on_a_block_wrapper_is_flagged() -> None:
    """A block's tags govern its children, so the defect one level up must still be seen."""
    doc = yaml.safe_load(
        """
        - name: The config and deploy pair
          tags: [config, deploy]
          register: block_result
          block:
            - name: Inner
              ansible.builtin.debug:
                msg: hi
        """
    )
    assert dual_tagged_producers(doc) == ["The config and deploy pair"]


def test_a_producer_tagged_always_is_clean() -> None:
    """The prescribed fix must not itself trip the rule."""
    doc = yaml.safe_load(
        """
        - name: Resolve the manifest destination
          ansible.builtin.set_fact:
            manifests_dest_dir: /etc/rancher/k3s/manifests
          tags: [always]
        """
    )
    assert dual_tagged_producers(doc) == []


def test_a_producer_with_one_split_tag_is_clean() -> None:
    doc = yaml.safe_load(
        """
        - name: Render the config
          ansible.builtin.set_fact:
            rendered: true
          tags: [config]
        """
    )
    assert dual_tagged_producers(doc) == []


def test_a_dual_tagged_plain_action_is_not_flagged() -> None:
    """Pins the narrowing. A dual tag on a task that hands nothing on is a selection choice.

    Widening the rule to every dual-tagged task means deleting this test, which is the point:
    the scope should not drift without someone deciding to drift it.
    """
    doc = yaml.safe_load(
        """
        - name: Restart the unit
          ansible.builtin.systemd:
            name: gitops-deploy
            state: restarted
          tags: [config, deploy]
        """
    )
    assert dual_tagged_producers(doc) == []


def test_a_scalar_tag_string_is_handled() -> None:
    """`tags: config` is legal YAML for a single tag, and must not crash the walk."""
    doc = yaml.safe_load(
        """
        - name: Render the config
          ansible.builtin.set_fact:
            rendered: true
          tags: config
        """
    )
    assert dual_tagged_producers(doc) == []
