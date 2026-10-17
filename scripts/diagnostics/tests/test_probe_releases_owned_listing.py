"""The plain `probe.py releases` listing and a tag Flux owns (#5309).

Flux applies a flux-owned tag from the OCI artifact, so the Ansible release record for it stops
being written and ages. `--stale-only` drops such a record before its verdict
(`releases_retired.drop_owned`); the plain listing must not print it `stale` either, or the two
views disagree about the same service.

Each test drives `run_releases` over a throwaway git repo in which origin/master has moved past
both records under each service's own role. Only the record read is stubbed; staleness, the
digest verdict and the owner read run for real, redirected at the fixture repo, an empty render
directory and a fixture host_vars file. The merge check and `missing_services` read the real
tree, which changes no `stale` flag on the two fixture services.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_owned_listing.py
"""

import functools
from types import SimpleNamespace

from diagnostics.probe_lib import releases as pr

from _release_fixtures import _commit, _init_repo, _record, _set_origin_master

import lib.k8s_roles

_FLUX, _ANSIBLE = "bento-pdf", "sonarr"


def _fixture(tmp_path, monkeypatch):
    """Two records at `base`, both stale by path at the tip; `bento-pdf` owned by Flux."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            f"ansible/roles/k8s/{svc}/templates/deployment.yaml.j2": "v1\n"
            for svc in (_FLUX, _ANSIBLE)
        },
        "v1",
    )
    tip = _commit(
        repo,
        {
            f"ansible/roles/k8s/{svc}/templates/deployment.yaml.j2": "v2\n"
            for svc in (_FLUX, _ANSIBLE)
        },
        "v2",
    )
    _set_origin_master(repo, tip)

    box = tmp_path / "daniel-box.yml"
    box.write_text(
        "containers_list:\n"
        f"  - name: {_FLUX}\n    platform: k8s\n    owner: flux\n"
        f"  - name: {_ANSIBLE}\n    platform: k8s\n"
    )
    records = [_record(_FLUX, commit=base), _record(_ANSIBLE, commit=base)]
    monkeypatch.setattr(pr, "load_records", lambda previous=False: list(records))
    real_tag_owners = lib.k8s_roles.tag_owners
    monkeypatch.setattr(
        lib.k8s_roles, "tag_owners", lambda host_vars=None: real_tag_owners(box)
    )
    monkeypatch.setattr(
        pr,
        "compute_stale",
        functools.partial(pr.compute_stale, repo_root=repo, shared_roles=set()),
    )
    monkeypatch.setattr(
        pr,
        "apply_digest_verdicts",
        functools.partial(
            pr.apply_digest_verdicts, repo_root=repo, render_dir=tmp_path / "renders"
        ),
    )


def _ns(**overrides):
    ns = dict(
        json=False,
        stale_only=False,
        service=None,
        previous=False,
        kuma=False,
        names_out=None,
        grace_minutes=0,
    )
    ns.update(overrides)
    return SimpleNamespace(**ns)


def _flags_by_service(text):
    """{service: [flags]} for each table row; the header and footer legend are skipped."""
    flags = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in (_FLUX, _ANSIBLE):
            flags[fields[0]] = fields[-1].split(",")
    return flags


def test_plain_listing_flags_an_old_ansible_record_but_not_a_flux_owned_one(
    tmp_path, monkeypatch, capsys
):
    _fixture(tmp_path, monkeypatch)

    pr.run_releases(_ns())

    flags = _flags_by_service(capsys.readouterr().out)
    assert "stale" in flags[_ANSIBLE], "the Ansible-owned record must still read stale"
    assert "stale" not in flags.get(_FLUX, []), (
        f"flux-owned {_FLUX} is printed stale from an Ansible record Flux no longer writes"
    )


def test_plain_listing_and_stale_only_count_the_same_stale_services(
    tmp_path, monkeypatch, capsys
):
    _fixture(tmp_path, monkeypatch)
    names_out = tmp_path / "names"

    pr.run_releases(_ns(stale_only=True, names_out=str(names_out)))
    capsys.readouterr()
    pr.run_releases(_ns())

    flags = _flags_by_service(capsys.readouterr().out)
    listed_stale = {svc for svc, f in flags.items() if "stale" in f}
    counted = set(names_out.read_text().split()) & {_FLUX, _ANSIBLE}
    assert counted == {_ANSIBLE}, "the fixture's --stale-only verdict is the oracle"
    assert listed_stale == counted
