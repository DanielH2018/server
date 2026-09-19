"""The closed citation grammar: a backticked span is support, rejected, or not a citation."""

import pytest

from facts.citations import FORMS, REJECT_REASONS, Citation, Rejected, parse_citations


def _one(text):
    cites, rejects = parse_citations(text)
    assert len(cites) + len(rejects) == 1, (cites, rejects)
    return (cites or rejects)[0]


def test_path_file_is_clean():
    c = _one("see `scripts/lib/kubectl.py` for the gate")
    assert c == Citation("path", "scripts/lib/kubectl.py", "scripts/lib/kubectl.py", "")


def test_path_directory_keeps_trailing_slash():
    c = _one("under `ansible/roles/k8s/traefik/`")
    assert c.form == "path" and c.path == "ansible/roles/k8s/traefik/"


def test_symbol_is_clean():
    c = _one("`scripts/lib/kubectl.py:run` refuses")
    assert c == Citation(
        "symbol", "scripts/lib/kubectl.py:run", "scripts/lib/kubectl.py", "run"
    )


def test_yaml_key_is_clean():
    c = _one("`ansible/roles/k8s/traefik/defaults/main.yml:traefik_replicas` is 2")
    assert c.form == "yaml"
    assert c.path == "ansible/roles/k8s/traefik/defaults/main.yml"
    assert c.selector == "traefik_replicas"


def test_yaml_dotted_key_is_clean():
    c = _one("`ansible/inventory/host_vars/daniel-box.yml:containers_list.0.name`")
    assert c.form == "yaml" and c.selector == "containers_list.0.name"


def test_test_node_is_clean():
    c = _one(
        "ENFORCED by `scripts/lib/tests/test_kubectl.py::test_refuses_other_cluster`"
    )
    assert c.form == "test"
    assert c.path == "scripts/lib/tests/test_kubectl.py"
    assert c.selector == "test_refuses_other_cluster"


def test_marker_is_clean():
    c = _one(
        "`ansible/roles/setup/gitops_deploy/files/deploy_logic.py:DECIDED: a fixed slice while`"
    )
    assert c.form == "marker"
    assert c.selector == "a fixed slice while"


def test_probe_is_clean():
    c = _one("`probe.py kuma-drift` answers what is missing")
    assert c == Citation("probe", "probe.py kuma-drift", "", "kuma-drift")


def test_probe_with_argument_is_clean():
    c = _one("`probe.py health traefik`")
    assert c.selector == "health traefik"


def test_file_line_is_flagged():
    r = _one("`deploy_logic.py:458` used to say")
    assert r == Rejected("deploy_logic.py:458", "file:line")


def test_file_line_range_is_flagged():
    assert _one("`CLAUDE.md:12-20`").reason == "file:line"


def test_bare_word_is_not_a_citation():
    assert parse_citations("run `sops` then `--dry-run` and `kubectl apply`") == (
        [],
        [],
    )


def test_bare_filename_without_slash_is_not_a_citation():
    assert parse_citations("`docker-compose.yml` is rendered") == ([], [])


def test_host_port_is_not_a_citation():
    assert parse_citations("listens on `127.0.0.1:3100`") == ([], [])


def test_fenced_code_is_skipped():
    text = "```\n`scripts/lib/git.py`\n```\nand `scripts/lib/gh.py`"
    cites, _ = parse_citations(text)
    assert [c.raw for c in cites] == ["scripts/lib/gh.py"]


def test_form_census():
    assert FORMS == frozenset({"path", "symbol", "yaml", "test", "marker", "probe"})
    assert REJECT_REASONS == frozenset({"file:line"})


@pytest.mark.parametrize("raw", ["scripts/lib/kubectl.py:run", "probe.py kuma-drift"])
def test_every_citation_round_trips_its_raw(raw):
    assert _one(f"`{raw}`").raw == raw
