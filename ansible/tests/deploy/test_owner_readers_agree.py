"""The three readers of D2's `owner` flag agree on what every k8s entry declares.

- `ansible_owned` in ansible/filter_plugins/toposort.py keeps the entries the play batches.
- `tag_owners` in scripts/lib/k8s_roles.py maps each non-Ansible-owned tag to its owner, for
  every script.
- `service_owners` in the deployer's deploy_inventory.py returns the same mapping from a
  stdlib regex parse, because the deployer runs under `uv run --no-project`.

Two predicates are compared. **Ansible-owned** is the tag set each reader leaves out: all
three answer it. **Published** is the tag set whose owner is `flux`: only the two mapping
readers answer it, because the filter keeps or drops an entry and never says why. Each
reader raises on any other owner value. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *The ownership flag and its three readers*.

Run: uv run pytest ansible/tests/deploy/test_owner_readers_agree.py
"""

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from ansible.errors import AnsibleFilterError
from deployer_lib import deploy_inventory
from deployer_lib.deploy_inventory import service_owners
from lib import k8s_roles, yaml_fast
from lib.k8s_roles import tag_owners
from lib.render_guard import entry_tags

import toposort
from _helpers import HOST_VARS
from toposort import ansible_owned

# One `flux` entry, one `released` entry with two tags, one Ansible-owned k8s entry, and a
# Docker entry, which no reader of the k8s plane judges.
FIXTURE = """\
containers_list:
  - name: bento-pdf
    platform: k8s
    owner: flux
  - name: n8n
    platform: k8s
    owner: released
    tags: [n8n, n8n-images]
  - name: sonarr
    platform: k8s
  - name: wg-easy
    port: 51821
"""


def _filter_not_owned(text: str) -> set[str]:
    """The k8s tags `ansible_owned` drops: every tag of an entry it leaves out."""
    containers = (yaml_fast.safe_load(text) or {}).get("containers_list") or []
    k8s = [c for c in containers if c.get("platform") == "k8s"]
    kept = {c["name"] for c in ansible_owned(k8s)}
    return {t for c in k8s if c["name"] not in kept for t in entry_tags(c)}


def _published(owners: dict[str, str]) -> set[str]:
    return {t for t, owner in owners.items() if owner == "flux"}


def _disagreements(
    path: Path, deployer: Callable[[str], dict[str, str]] = service_owners
) -> list[str]:
    """Each predicate on which the three readers of `path` differ, named with each answer."""
    text = path.read_text()
    by_filter = _filter_not_owned(text)
    by_script = tag_owners(path)
    by_deployer = deployer(text)
    found = []
    if not by_filter == by_script.keys() == by_deployer.keys():
        found.append(
            f"not Ansible-owned: filter {sorted(by_filter)}, script {sorted(by_script)}, "
            f"deployer {sorted(by_deployer)}"
        )
    if _published(by_script) != _published(by_deployer):
        found.append(
            f"published: script {sorted(_published(by_script))}, "
            f"deployer {sorted(_published(by_deployer))}"
        )
    return found


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "daniel-box.yml"
    path.write_text(text)
    return path


def test_the_three_readers_agree_on_a_flux_and_a_released_entry_is_clean(tmp_path):
    path = _write(tmp_path, FIXTURE)
    assert _disagreements(path) == []
    assert service_owners(FIXTURE) == {
        "bento-pdf": "flux",
        "n8n": "released",
        "n8n-images": "released",
    }


def test_a_reader_that_misses_released_is_flagged(tmp_path):
    """The census's red half: a deployer reader that knows only `flux` disagrees."""
    path = _write(tmp_path, FIXTURE)

    def flux_only(text: str) -> dict[str, str]:
        return {t: o for t, o in service_owners(text).items() if o == "flux"}

    assert _disagreements(path, flux_only) == [
        "not Ansible-owned: filter ['bento-pdf', 'n8n', 'n8n-images'], script "
        "['bento-pdf', 'n8n', 'n8n-images'], deployer ['bento-pdf']"
    ]


def test_the_three_readers_agree_on_the_live_inventory():
    hosts = sorted(p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_"))
    assert "daniel-box.yml" in {p.name for p in hosts}
    for path in hosts:
        assert _disagreements(path) == [], path.name


def test_every_live_k8s_entry_is_reached_by_all_three_readers(tmp_path):
    """Each live entry given `owner: flux` is read as such by every reader.

    No live entry carries `owner` until D2.7, so the live census compares empty sets. This
    sets the flag on every entry of daniel-box's list, so a regex that stops reaching an
    entry, for a quoted name or a field it cannot step over, fails here rather than reading
    that entry as Ansible-owned after its flip.
    """
    live = (HOST_VARS / "daniel-box.yml").read_text()
    flagged = re.sub(r"^(  - name: .*)$", r"\1\n    owner: flux", live, flags=re.M)
    path = _write(tmp_path, flagged)
    assert _disagreements(path) == []
    tags = service_owners(flagged)
    assert {"bento-pdf", "traefik", "authelia"} <= tags.keys()
    assert len(tags) >= 50
    assert set(tags.values()) == {"flux"}


@pytest.mark.parametrize("value", ["ansible", "Flux", "", "~", "[flux]"])
def test_each_reader_raises_on_any_other_owner_value(tmp_path, value: str):
    text = FIXTURE.replace("owner: flux", f"owner: {value}")
    path = _write(tmp_path, text)
    with pytest.raises(ValueError, match="bento-pdf"):
        tag_owners(path)
    with pytest.raises(ValueError, match="bento-pdf"):
        service_owners(text)
    with pytest.raises(AnsibleFilterError, match="bento-pdf"):
        _filter_not_owned(text)


def test_the_three_readers_accept_the_same_owner_values():
    assert (
        deploy_inventory.OWNER_VALUES
        == k8s_roles.OWNER_VALUES
        == toposort.OWNER_VALUES
        == {"flux", "released"}
    )
