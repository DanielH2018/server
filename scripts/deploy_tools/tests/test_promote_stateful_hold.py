"""D5.2b1's stateful hold: `decide`'s record of changed snapshot roles, and `promote.py hold`.

The `settle` half, which writes `<T> stateful=<names>` and leaves `prod`, runs as shell in
`test_promote_settle.py`. Here `decide` runs over the fake forge in `_promote_fakes.py`, with
snapshot Jobs rendered by the renderer's own `snapshot_files`, so a renamed env variable fails
here too. The post runs through `promote.main` with a fake sender in place of Discord.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_stateful_hold.py
"""

import json

import forge
import promote
import pytest
from _promote_fakes import NOW, World, bundle_files, digest_of
from deploy_tools.promote_lib import stateful
from renderer.renderer_lib.snapshot import snapshot_files

SONARR, RADARR = "1111aaaa", "2222bbbb"


def _snapshot(tag: str, digest8: str, claims: dict[str, str]) -> dict[str, bytes]:
    files = snapshot_files(
        tag, digest8, claims, "img:1", 3, 600, 5, 540, "script", "helper"
    )
    return {f"cluster/{tag}-snapshot/{n}": t.encode() for n, t in files.items()}


def _bundle(own: dict[str, str], footprint: dict[str, str] | None = None, **extra):
    """A bundle whose `digests.json` records `own` per tag, plus `extra` files by path."""
    footprint = footprint or {}
    digests = {
        tag: {"own": value, "footprint": footprint.get(tag, value)}
        for tag, value in own.items()
    } | {"_unclassified": {"own": "u", "footprint": "u"}}
    files = bundle_files(digests)
    for part in extra.values():
        files.update(part)
    return files


SONARR_SNAP = _snapshot("sonarr", SONARR, {"sonarr-config": "media"})


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, size=2)
    base = w.commits[-1]
    w.set_prod(base, files=_bundle({"sonarr": "s1", "traefik": "t1"}))
    w.set_last_good(base)
    return w


def _decide(world, monkeypatch, capsys, bundle_dir):
    output = world.tmp_path / "github_output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["decide", "--repo", str(world.repo), "--master-ref", "master"]
    argv += ["--bundle-dir", str(bundle_dir)]
    code = promote.main(argv, f=world.clients(), now=NOW)
    capsys.readouterr()
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return code, outputs


# -- decide: which revisions are stateful ----------------------------------------------------


def test_a_revision_that_changes_a_snapshot_roles_own_digest_is_stateful(
    world, monkeypatch, capsys, tmp_path
):
    r = world.commit("bump sonarr")
    world.publish(r, files=_bundle({"sonarr": "s2", "traefik": "t1"}, snap=SONARR_SNAP))
    code, outputs = _decide(world, monkeypatch, capsys, tmp_path / "handoff")
    assert (code, outputs["verdict"], outputs["stateful"]) == (0, "promote", "sonarr")
    found = stateful.read(tmp_path / "handoff")
    assert found["digest"] == digest_of(r), "the digest the fixture registry names"
    assert found["stateful"] == {
        "sonarr": {
            "digest8": SONARR,
            "claims": [{"name": "sonarr-config", "namespace": "media"}],
        }
    }


@pytest.mark.parametrize(
    "own, footprint",
    [({"sonarr": "s1", "traefik": "t2"}, {}), ({"sonarr": "s1"}, {"sonarr": "f2"})],
    ids=["another-tag-changed", "footprint-only"],
)
def test_a_revision_that_leaves_every_snapshot_roles_own_digest_is_not_stateful(
    world, monkeypatch, capsys, tmp_path, own, footprint
):
    """`own` renames the snapshot Job, so a role whose `own` is unchanged took no snapshot."""
    own.setdefault("traefik", "t1")
    r = world.commit("change something else")
    world.publish(r, files=_bundle(own, footprint, snap=SONARR_SNAP))
    code, outputs = _decide(world, monkeypatch, capsys, tmp_path / "handoff")
    assert (code, outputs["verdict"], outputs["stateful"]) == (0, "promote", "")


def test_an_unreadable_snapshot_job_is_an_error_and_moves_nothing(
    world, monkeypatch, capsys, tmp_path
):
    r = world.commit("break the snapshot job")
    broken = {"cluster/sonarr-snapshot/job.yaml": b"kind: Job\nspec: {}\n"}
    world.publish(r, files=_bundle({"sonarr": "s2", "traefik": "t1"}, snap=broken))
    code, outputs = _decide(world, monkeypatch, capsys, tmp_path / "handoff")
    assert (code, outputs["verdict"]) == (1, "error")


# -- the post ---------------------------------------------------------------------------------

T = "a" * 40
DIGEST = "sha256:" + "d" * 64


def _record(tmp_path, failed_soak_job: str = "") -> object:
    target = _bundle(
        {"sonarr": "s2", "radarr": "r2", "traefik": "t2"},
        a=SONARR_SNAP,
        b=_snapshot(
            "radarr", RADARR, {"radarr-config": "media", "radarr-backups": "media"}
        ),
    )
    if failed_soak_job:
        target["cluster/soak/job.yaml"] = (
            f"apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: {failed_soak_job}\n"
        ).encode()
    base = json.loads(_bundle({"sonarr": "s1", "radarr": "r1"})["digests.json"])
    found = stateful.record(target, json.loads(target["digests.json"]), base, DIGEST)
    stateful.write(tmp_path, found)
    return found


def _hold(tmp_path, failed: str, post=None) -> tuple[int, list[str]]:
    sent: list[str] = []
    argv = [
        "hold",
        T,
        "--hold",
        f"{T} stateful={failed}",
        "--bundle-dir",
        str(tmp_path),
    ]
    argv += ["--verdict", "failed", "--failed", failed]
    code = promote.main(argv, post=post or sent.append)
    return code, sent


def test_two_changed_snapshot_roles_each_get_a_whole_block_and_the_failed_one_is_marked(
    tmp_path,
):
    """D5 Open question 1, case (b): a block for every changed snapshot role.

    The red half is a post that carries a block for the failed role only.
    """
    _record(tmp_path)
    code, sent = _hold(tmp_path, "radarr")
    assert code == 0
    header, *roles = sent
    assert [m.split("\n", 1)[0] for m in roles] == [
        f"promote hold {T[:8]}: snapshot role radarr (FAILED), digest8 {RADARR}",
        f"promote hold {T[:8]}: snapshot role sonarr (changed), digest8 {SONARR}",
    ]
    radarr, sonarr = roles
    assert "volume_revert_sha=2222bbbb" in radarr and "radarr-backups" in radarr
    assert "volume_revert_service=sonarr" in sonarr
    assert "volume_revert_sha=1111aaaa" in sonarr and "radarr" not in sonarr
    assert (
        "radarr (snapshot role)" in header and "traefik (not a snapshot role)" in header
    )
    assert "prod was NOT reset" in header


def test_a_failing_non_snapshot_role_is_marked_and_the_snapshot_roles_still_post(
    tmp_path,
):
    """D5 Open question 1, case (a): traefik broke, yet every changed snapshot role posts."""
    _record(tmp_path)
    code, sent = _hold(tmp_path, "traefik")
    assert code == 0 and len(sent) == 3
    assert "Failed or timed out: traefik (not a snapshot role)" in sent[0]


def test_each_claim_of_a_role_gets_its_snapshot_prefix(tmp_path):
    _record(tmp_path)
    _, sent = _hold(tmp_path, "radarr")
    radarr = sent[1]
    assert "- radarr-backups (media): autodeploy-radarr-2222bbbb-backups-" in radarr
    assert "- radarr-config (media): autodeploy-radarr-2222bbbb-config-" in radarr


def test_the_classify_command_carries_the_prod_digest_string_not_the_sha(tmp_path):
    """The red halves: a comparison value that is R's SHA, and one that is missing."""
    _record(tmp_path)
    _, sent = _hold(tmp_path, "sonarr")
    for message in sent[1:]:
        classify = next(line for line in message.splitlines() if "1. classify" in line)
        assert classify.endswith(f"prints prod@{DIGEST}")
        assert T not in classify
        assert (
            "jsonpath='{.status.lastAttemptedRevision}'"
            in message.split("1. classify")[1]
        )


def test_a_soak_failure_names_the_soak_job_and_its_read_command(tmp_path):
    _record(tmp_path, failed_soak_job="soak-0badc0de")
    _, sent = _hold(tmp_path, "soak")
    assert "soak failed. Its Job is soak-0badc0de" in sent[0]
    assert "get pods -l job-name=soak-0badc0de -o jsonpath=" in sent[0]


def test_every_message_fits_discord_with_long_names_and_many_claims(tmp_path):
    """Names at Longhorn's 63-byte snapshot-name limit, and six claims on one role."""
    tag = "exportarr-sonarr"
    claims = {f"{tag}-configdat-{n}": "media-namespace" for n in range(6)}
    target = _bundle(
        {tag: "x2", "sonarr": "s2"}, a=SONARR_SNAP, b=_snapshot(tag, "3333cccc", claims)
    )
    found = stateful.record(target, json.loads(target["digests.json"]), None, DIGEST)
    sent = stateful.messages(T, f"{T} stateful=sonarr", "failed", ["sonarr"], found)
    assert all(len(m) <= forge.DISCORD_MAX for m in sent), [len(m) for m in sent]
    assert all(m.count("```") in (0, 2) for m in sent), "a block was cut"
    blocks = [m for m in sent if "```" in m and "1. classify" in m]
    assert len(blocks) == 2 and "configdat-5" in blocks[0], "every role keeps its block"


def test_a_failed_post_fails_the_run(tmp_path):
    _record(tmp_path)

    def refuse(content: str) -> None:
        raise forge.ForgeError("the Discord webhook answered HTTP 500")

    code, _ = _hold(tmp_path, "sonarr", post=refuse)
    assert code == 1


def test_a_refused_post_is_an_error_that_does_not_name_the_webhook():
    url = "https://discord.com/api/webhooks/1/secret-token"
    sent = []

    def answer(target, body, headers):
        sent.append(json.loads(body))
        return forge.Response(500)

    with pytest.raises(forge.ForgeError) as err:
        forge.Discord(url, send=answer).post("hello")
    assert sent == [{"content": "hello"}]
    assert "secret-token" not in str(err.value)
    assert forge.Discord(url, send=lambda *a: forge.Response(204)).post("x") is None


def test_an_unreachable_webhook_is_an_error_that_does_not_name_it():
    with pytest.raises(forge.ForgeError) as err:
        forge.urllib_post("http://127.0.0.1:9/api/webhooks/secret-token", b"{}", {}, 2)
    assert "secret-token" not in str(err.value) and err.value.__cause__ is None
