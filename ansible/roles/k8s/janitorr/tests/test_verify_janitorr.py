"""janitorr's Flux verify Job: each check fails its broken case, and the Job sees what janitorr does.

The script tests run `verify_janitorr.main` against a config file, a stand-in for the three
apps, and a media tree under `tmp_path`. The render tests render a copy of the role, so a test
can edit the copy and leave the tree alone. The plan is
docs/target-architecture/deploy/d5-bulk-move.md.

Run: uv run pytest ansible/roles/k8s/janitorr/tests/test_verify_janitorr.py
"""

import os
import re
import shutil
import socket
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import verify_janitorr
from flux_job_connect import open_url
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import ANSIBLE
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.restart import pod_references
from renderer.tests._job_census import problems

TAG = "janitorr"
PROBE = verify_janitorr.PROBE_NAME
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")
# The config's two keys under `application:`, as the template writes them.
LIVE_CONFIG = "application:\n  dry-run: false\n  leaving-soon: 14d\n"
DRY_CONFIG = "application:\n  dry-run: true\n  leaving-soon: 14d\n"


def _serve() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Jellyfin's /health is a 503 while it is busy, and that is still an answer.
            self.send_response(200 if self.path == "/ping" else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return server


@pytest.fixture
def apps():
    server = _serve()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield f"{base}/ping {base}/ping {base}/health"
    server.shutdown()
    server.server_close()


@pytest.fixture
def media(tmp_path):
    """jellyfin's view of the library: `MEDIA_ROOT/media/tv` holds one episode."""
    root = tmp_path / "data"
    episode = root / "media" / "tv" / "Show" / "Season 01" / "e01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"\x1a\x45\xdf\xa3" * 512)
    return root


def _env(tmp_path, apps, media, config=LIVE_CONFIG, expect="false") -> dict:
    path = tmp_path / "application.yml"
    path.write_text(config)
    return {
        "JANITORR_CONFIG": str(path),
        "JANITORR_EXPECT_DRY_RUN": expect,
        "JANITORR_TARGETS": apps,
        "MEDIA_ROOT": str(media),
    }


def _probe(media) -> str:
    return str(media / "media" / "leaving-soon" / "tv" / PROBE)


def test_a_live_pod_that_reaches_the_apps_and_resolves_its_link_passes(
    tmp_path, apps, media, capsys
):
    assert verify_janitorr.main([], _env(tmp_path, apps, media)) == 0
    out = capsys.readouterr().out
    assert "dry-run ok: janitorr's config says `dry-run: false`" in out
    assert out.count("reach ok:") == 3
    assert "leaving-soon ok:" in out
    assert not os.path.lexists(_probe(media))


def test_a_live_config_fails_when_the_role_renders_janitorr_dry(
    tmp_path, apps, media, capsys
):
    env = _env(tmp_path, apps, media, expect="true")
    assert verify_janitorr.main([], env) == 1
    assert "it would delete real media" in capsys.readouterr().err
    env = _env(tmp_path, apps, media, config=DRY_CONFIG, expect="true")
    assert verify_janitorr.main([], env) == 0


def test_a_config_with_no_dry_run_line_fails(tmp_path, apps, media, capsys):
    env = _env(tmp_path, apps, media, config="application:\n  leaving-soon: 14d\n")
    assert verify_janitorr.main([], env) == 1
    assert "has no `  dry-run:` line" in capsys.readouterr().err


def test_a_refused_app_fails_the_reach_check():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    opener = partial(open_url, retry_seconds=0)
    with pytest.raises(verify_janitorr.Failed, match="UNREACHABLE"):
        verify_janitorr.reach(f"http://127.0.0.1:{port}/ping", opener)


def test_a_link_whose_target_does_not_resolve_fails_and_is_removed(
    tmp_path, apps, media, capsys
):
    """The mount-depth bug's symptom: a link jellyfin lists and cannot play."""
    episode = next((media / "media" / "tv").rglob("*.mkv"))
    episode.unlink()
    episode.symlink_to(tmp_path / "gone.mkv")
    assert verify_janitorr.main([], _env(tmp_path, apps, media)) == 1
    assert "does not resolve in jellyfin's mount layout" in capsys.readouterr().err
    assert not os.path.lexists(_probe(media))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 file")
def test_a_target_that_resolves_but_cannot_be_read_fails(tmp_path, apps, media, capsys):
    next((media / "media" / "tv").rglob("*.mkv")).chmod(0)
    assert verify_janitorr.main([], _env(tmp_path, apps, media)) == 1
    assert "resolves but cannot be read" in capsys.readouterr().err
    assert not os.path.lexists(_probe(media))


def test_a_probe_a_killed_run_left_behind_is_replaced(tmp_path, apps, media, capsys):
    probe = media / "media" / "leaving-soon" / "tv" / PROBE
    probe.parent.mkdir(parents=True)
    probe.symlink_to(tmp_path / "stale.mkv")
    assert verify_janitorr.main([], _env(tmp_path, apps, media)) == 0
    assert "removed a probe a killed run left" in capsys.readouterr().out
    assert not os.path.lexists(probe)


def test_a_library_with_no_episode_fails(tmp_path, apps, capsys):
    (tmp_path / "data" / "media" / "tv").mkdir(parents=True)
    assert verify_janitorr.main([], _env(tmp_path, apps, tmp_path / "data")) == 1
    assert "no .mkv under" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _docs(texts: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(texts.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _render_tree_copy(tmp_path, stubs, edit=None) -> cluster.RenderedTag:
    """The flipped render of a copy of the role and of `cluster-vars.source`.

    `edit(role)` changes the copied role first. `playbook_dir` rides in the stubs, which outrank
    every other layer, so the Job's lookups read the copy rather than the tree. The copy links
    the tree's setup roles, which the health ConfigMap reads.
    """
    tree = tmp_path / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    # The health ConfigMap ships setup/common's host_lib.py and kuma_push.py by playbook_dir.
    (tree / "roles" / "setup").symlink_to(ANSIBLE / "roles" / "setup")
    (tree / "vars").mkdir()
    shutil.copy(ANSIBLE / "vars" / "cluster-vars.source", tree / "vars")
    if edit:
        edit(role)
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s")


@pytest.fixture(scope="module")
def flipped_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    return _render_tree_copy(tmp_path_factory.mktemp("flipped"), stubs)


@pytest.fixture(scope="module")
def flipped(flipped_tag) -> list[dict]:
    return _docs(flipped_tag.verify)


def _job(docs: list[dict]) -> dict:
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    return job


def _env_of(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {env["name"]: env["value"] for env in container["env"]}


def _deployment(tag: str, stubs) -> dict:
    (deployment,) = [
        doc
        for doc in _docs(cluster.render_tag(tag, stubs).files)
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == tag
    ]
    return deployment


def _mounts(pod: dict, container: str, volume: str) -> list[tuple[str, str | None]]:
    """`(mountPath, subPath)` of each mount of `volume` in `container`, in order."""
    (spec,) = [c for c in pod["containers"] if c["name"] == container]
    return [
        (m["mountPath"], m.get("subPath"))
        for m in spec["volumeMounts"]
        if m["name"] == volume
    ]


def _claim(pod: dict, volume: str) -> str:
    (source,) = [v for v in pod["volumes"] if v["name"] == volume]
    return source["persistentVolumeClaim"]["claimName"]


def test_the_tree_renders_janitorr_verify(stubs, flipped_tag):
    """The flip moved the Job to `templates/verify/`, so the tree's own render emits it."""
    assert cluster.render_tag(TAG, stubs).verify == flipped_tag.verify != {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    assert sorted(doc["kind"] for doc in flipped) == [
        "ConfigMap",
        "Job",
        "ServiceAccount",
    ]
    assert problems({f"{TAG}-verify": flipped}) == []


def test_the_census_fails_a_flipped_job_once_its_spec_moves(flipped):
    """The red case for the name row: an edited spec under the rendered name."""
    docs = [dict(doc) for doc in flipped]
    job = _job(docs)
    job["spec"] = {**job["spec"], "backoffLimit": 1}
    assert any(line.startswith("name: ") for line in problems({f"{TAG}-verify": docs}))


@pytest.mark.parametrize("fence,port", [("sonarr", 8989), ("radarr", 7878)])
def test_the_arr_fences_admit_janitorr(fence, port, flipped, stubs):
    """Each fence admits the Job's pod and janitorr's own, on the app's port.

    The Job proves the apps answer `janitorr-verify`, so this also holds `janitorr` admitted.
    The `janitorr-verify` peers landed in a PR of their own (#5709), because the cross-plane
    gate refuses them beside a janitorr change.
    """
    (policy,) = [
        doc
        for doc in _docs(cluster.render_tag(fence, stubs).files)
        if doc["kind"] == "NetworkPolicy"
    ]
    admitted = {
        (peer["podSelector"]["matchLabels"]["app"], int(rule_port["port"]))
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from") or []
        if "matchLabels" in (peer.get("podSelector") or {})
        for rule_port in rule["ports"]
    }
    job_app = _job(flipped)["spec"]["template"]["metadata"]["labels"]["app"]
    assert job_app == "janitorr-verify"
    assert {(TAG, port), (job_app, port)} <= admitted


def test_the_job_sees_the_library_where_janitorr_writes_and_jellyfin_reads(
    flipped, stubs
):
    """The probe is janitorr's link at janitorr's place, read through jellyfin's mounts."""
    job_pod = _job(flipped)["spec"]["template"]["spec"]
    janitorr = _deployment(TAG, stubs)["spec"]["template"]["spec"]
    jellyfin = _deployment("jellyfin", stubs)["spec"]["template"]["spec"]
    # janitorr's namespace: the whole claim at /data, so `media/tv` is `/data/media/tv`.
    assert _mounts(janitorr, TAG, "media") == [("/data", None)]
    # jellyfin's namespace puts the same `media` directory at `/data/media` as well.
    assert _mounts(jellyfin, "jellyfin", "media") == [
        ("/data", "media"),
        ("/data/media", "media"),
    ]
    assert _mounts(job_pod, "verify", "media") == _mounts(jellyfin, "jellyfin", "media")
    claims = {_claim(pod, "media") for pod in (job_pod, janitorr, jellyfin)}
    assert claims == {"media-data"}
    # Where janitorr writes its links and where it tells jellyfin they are.
    config = (
        K8S_ROLES / TAG / "templates" / "config" / "application.yml.j2"
    ).read_text()
    for key in ("leaving-soon-dir", "media-server-leaving-soon-dir"):
        assert f'  {key}: "/data/media/leaving-soon"' in config, key
    assert _env_of(_job(flipped))["MEDIA_ROOT"] == "/data"


def test_the_job_reads_janitorrs_config_and_dials_the_urls_it_names(flipped, stubs):
    job_pod = _job(flipped)["spec"]["template"]["spec"]
    janitorr = _deployment(TAG, stubs)["spec"]["template"]["spec"]
    assert _mounts(job_pod, "verify", "janitorr-config") == _mounts(
        janitorr, TAG, "janitorr-config"
    )
    config = (
        K8S_ROLES / TAG / "templates" / "config" / "application.yml.j2"
    ).read_text()
    named = re.findall(r'^    url: "([^"]+)"$', config, re.M)
    targets = _env_of(_job(flipped))["JANITORR_TARGETS"].split()
    assert [url.rsplit("/", 1)[0] for url in targets] == named


def _make_dry(role) -> None:
    defaults = role / "defaults" / "main.yml"
    text = defaults.read_text()
    assert 'janitorr_k8s_dry_run: "false"' in text
    defaults.write_text(text.replace('dry_run: "false"', 'dry_run: "true"'))


def test_the_job_expects_the_dry_run_mode_the_role_default_sets(
    flipped, stubs, tmp_path
):
    """The Job expects what the Secret it mounts renders, from the one default."""
    assert _env_of(_job(flipped))["JANITORR_EXPECT_DRY_RUN"] == "false"
    dry_tag = _render_tree_copy(tmp_path, stubs, _make_dry)
    assert _env_of(_job(_docs(dry_tag.verify)))["JANITORR_EXPECT_DRY_RUN"] == "true"
    (secret,) = [
        doc
        for doc in _docs(dry_tag.files)
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == "janitorr-config"
    ]
    assert "\n  dry-run: true\n" in secret["stringData"]["application.yml"]


def _edit_the_policy(role) -> None:
    config = role / "templates" / "config" / "application.yml.j2"
    config.write_text(config.read_text() + "# a policy-only edit\n")


def _rotate_the_sonarr_key(role) -> None:
    source = role.parents[2] / "vars" / "cluster-vars.source"
    lines = source.read_text().splitlines()
    (row,) = [n for n, line in enumerate(lines) if line.startswith("sonarr_api_key ")]
    key, _digest, *rest = lines[row].split()
    lines[row] = " ".join([key, "0" * 64, *rest])
    source.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize(
    "edit", [_edit_the_policy, _rotate_the_sonarr_key], ids=["policy", "rotation"]
)
def test_a_change_that_rolls_the_pod_renames_the_job(edit, flipped, stubs, tmp_path):
    """Each rolls janitorr through `checksum/restart` and leaves the Deployment's text alone."""
    after = _docs(_render_tree_copy(tmp_path, stubs, edit).verify)
    before_env, after_env = _env_of(_job(flipped)), _env_of(_job(after))
    assert before_env["JANITORR_ROLL_DIGEST8"] != after_env["JANITORR_ROLL_DIGEST8"]
    assert _job(flipped)["metadata"]["name"] != _job(after)["metadata"]["name"]


def test_every_input_the_pod_names_is_one_the_roll_digest_reads(flipped_tag):
    """A ConfigMap or Secret the pod gains moves `checksum/restart`, so the digest must read it."""
    sources = {
        (doc["kind"], doc["metadata"]["name"]): f"{name}.j2"
        for name, text in flipped_tag.files.items()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") in ("ConfigMap", "Secret")
    }
    (deployment,) = [
        doc for doc in _docs(flipped_tag.files) if doc["kind"] == "Deployment"
    ]
    named = {sources[ref] for ref in pod_references(deployment["spec"]["template"])}
    assert named == {"config-secret.yaml.j2"}
    text = (K8S_ROLES / TAG / "templates" / "verify" / "job.yaml.j2").read_text()
    assert {name for name in named if f"/templates/{name}'" not in text} == set()


def test_the_job_reads_no_other_roles_file():
    """The helper and the image come from `render_tag`, never a lookup into another role."""
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []


def test_the_rendered_config_trims_media_at_longhorns_25_percent_floor(flipped_tag):
    """#5806: the loosest rung is 25% free, so janitorr acts before Longhorn stops scheduling."""
    (secret,) = [
        doc
        for doc in _docs(flipped_tag.files)
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == "janitorr-config"
    ]
    deletion = yaml_fast.safe_load(secret["stringData"]["application.yml"])[
        "application"
    ]["media-deletion"]
    assert deletion["movie-expiration"][25] == "180d"
    assert deletion["season-expiration"][25] == "240d"
    for rungs in (deletion["movie-expiration"], deletion["season-expiration"]):
        assert max(rungs) == 25
