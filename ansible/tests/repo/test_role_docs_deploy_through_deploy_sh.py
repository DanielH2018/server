"""A role CLAUDE.md gives its deploy command as `./scripts/deploy.sh`, never the bare playbook.

The root CLAUDE.md routes every deploy through `scripts/deploy.sh`, which takes the tree lock
the GitOps deployer and the secret-rotate cron share, snapshots HEAD, and checks the tag and
staleness before it runs the playbook. A bare `uv run ansible-playbook ansible/deploy.yml` does
none of that. The 2026-09-26 CLAUDE.md audit (#2678) found 38 role docs giving the bare form in
their Editing section, spread by copying a sibling rather than by any scaffold, so a guard is
what stops the next copy.

Run: uv run pytest ansible/tests/repo/test_role_docs_deploy_through_deploy_sh.py
"""

import re

from _helpers import ROLES

BARE_PLAYBOOK = re.compile(r"ansible-playbook\s+ansible/deploy\.yml")

# A census that globbed nothing would pass; these docs must be among the ones it reads.
KNOWN_ROLE_DOCS = frozenset(
    {"k8s/sonarr", "k8s/traefik", "containers/wg-easy", "setup/k3s"}
)


def bare_playbook_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if BARE_PLAYBOOK.search(line)]


def role_docs() -> dict[str, str]:
    return {
        f"{doc.parent.parent.name}/{doc.parent.name}": doc.read_text()
        for doc in sorted(ROLES.glob("*/*/CLAUDE.md"))
    }


def test_a_deploy_sh_line_is_clean():
    assert bare_playbook_lines('- Deploy: `./scripts/deploy.sh --tags "sonarr"`') == []


def test_a_bare_playbook_line_is_flagged():
    line = '- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "sonarr"`'
    assert bare_playbook_lines(line) == [line.strip()]


def test_the_census_reads_the_known_role_docs():
    missing = KNOWN_ROLE_DOCS - role_docs().keys()
    assert not missing, f"role-doc glob no longer finds: {sorted(missing)}"


def test_no_role_doc_deploys_through_the_bare_playbook():
    offenders = {
        role: lines
        for role, text in role_docs().items()
        if (lines := bare_playbook_lines(text))
    }
    assert not offenders, (
        'Deploy through ./scripts/deploy.sh --tags "<svc>" (add -e target=daniel-pi for a '
        "Pi service), not the bare playbook:\n  "
        + "\n  ".join(
            f"{role}: {line}" for role, lines in offenders.items() for line in lines
        )
    )
