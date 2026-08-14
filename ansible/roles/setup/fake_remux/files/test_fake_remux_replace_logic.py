import importlib.util
import json
import pathlib
import sys

# Load the host script's pure core directly (not a package), mirroring test_fake_remux_logic.py.
_SPEC = importlib.util.spec_from_file_location(
    "fake_remux_replace_logic",
    pathlib.Path(__file__).with_name("fake_remux_replace_logic.py"),
)
rl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rl)

# The shell resolves its sibling imports via sys.path.insert(0, <own dir>), same as fake_remux_scan.py
# itself — importing it here (rather than another importlib.util load) exercises that resolution.
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fake_remux_replace as sh  # noqa: E402

POLICY = {
    "deny_release_groups": ["NTRX"],
    "prefer_release_groups": ["VARYG"],
    "prefer_indexers": ["Nyaa.si", "Knaben"],
    "depreference_codecs": ["av1"],
    "min_size_gb": 0.4,
    "max_size_gb": 4.0,
    "searches_per_tick": 2,
}


def _rel(title, qname, qid, cf, seeders, indexer="Nyaa.si", rej=None, size=1.4e9):
    return {
        "title": title,
        "quality": {"quality": {"name": qname, "id": qid}},
        "qualityWeight": qid,
        "customFormatScore": cf,
        "seeders": seeders,
        "indexer": indexer,
        "rejections": rej or [],
        "size": size,
        "guid": "g:" + title,
    }


def test_rejects_deny_group_wrongmap_blocked_size():
    cands = [
        _rel("Show S02E13 NTRX", "Bluray-1080p Remux", 20, 500, 300),
        _rel(
            "Show 13 LostYears",
            "WEBDL-1080p",
            15,
            400,
            200,
            rej=["Episode wasn't requested: 1x13"],
        ),
        _rel(
            "Show S02E13 dead",
            "WEBDL-1080p",
            15,
            400,
            200,
            rej=["Indexer Nyaa.si is blocked till 2026"],
        ),
        _rel("Show S02E13 tiny", "WEBDL-1080p", 15, 400, 200, size=0.2e9),
    ]
    rel, reason = rl.select_replacement(cands, POLICY)
    assert rel is None
    assert "candidate" in reason


def test_accepts_genuine_hevc_and_2160p_no_codec_or_res_reject():
    cands = [
        _rel("Show S02E13 x265 WEB", "WEBDL-1080p", 15, 30, 40),
        _rel("Show S02E13 2160p", "WEBDL-2160p", 25, 20, 50),
    ]
    rel, _ = rl.select_replacement(cands, POLICY)
    # 2160p is a higher tier → chosen; neither is rejected for codec/resolution
    assert rel["quality"]["quality"]["name"] == "WEBDL-2160p"


def test_ranks_cf_then_seeders_within_tier():
    cands = [
        _rel("Show S02E13 CR VARYG", "WEBDL-1080p", 15, 506, 12, indexer="Knaben"),
        _rel("Show S02E13 other", "WEBDL-1080p", 15, 5, 600),
    ]
    rel, _ = rl.select_replacement(cands, POLICY)
    assert rel["customFormatScore"] == 506  # CF beats seeders within a tier


def test_av1_depreferenced_below_equal_tier_alternative():
    cands = [
        _rel("Show S02E13 AV1 pack", "WEBDL-1080p", 15, 506, 300),
        _rel("Show S02E13 h264", "WEBDL-1080p", 15, 200, 20),
    ]
    rel, _ = rl.select_replacement(cands, POLICY)
    assert (
        "h264" in rel["title"]
    )  # non-AV1 wins within the tier despite lower CF/seeders


def test_av1_wins_when_only_option():
    cands = [_rel("Show S02E13 AV1 only", "WEBDL-1080p", 15, 506, 300)]
    rel, _ = rl.select_replacement(cands, POLICY)
    assert rel is not None and "AV1" in rel["title"]


def test_x265_candidate_is_not_codec_rejected():
    cands = [_rel("Show S02E13 x265", "WEBDL-1080p", 15, 400, 200)]
    rel, _ = rl.select_replacement(cands, POLICY)
    assert rel is not None and "x265" in rel["title"]


def test_av1_higher_tier_still_beats_lower_tier_non_av1():
    cands = [
        _rel("Show S02E13 AV1 2160p", "WEBDL-2160p", 25, 20, 50),
        _rel("Show S02E13 h264 1080p", "WEBDL-1080p", 15, 506, 300),
    ]
    rel, _ = rl.select_replacement(cands, POLICY)
    assert rel["quality"]["quality"]["name"] == "WEBDL-2160p"


def test_size_zero_unknown_passes():
    cands = [_rel("Show S02E13 unknown size", "WEBDL-1080p", 15, 400, 200, size=0)]
    rel, _ = rl.select_replacement(cands, POLICY)
    assert rel is not None


def test_authentic_remux_reencode_fails_regardless_of_codec():
    # claims Remux but ships an x265 re-encoder tag → not authentic
    probe = {
        "quality": "Bluray-1080p Remux",
        "encoder": "x265",
        "keyframes": [],
        "window_s": 40,
    }
    assert not rl.is_authentic(probe, POLICY)


def test_authentic_genuine_remux_short_gop_passes():
    probe = {
        "quality": "Bluray-1080p Remux",
        "encoder": None,
        "keyframes": [0.0, 1.0, 2.0, 3.0],
        "window_s": 40,
    }
    assert rl.is_authentic(probe, POLICY)


def test_authentic_webdl_encode_is_fine():
    # a WEB-DL legitimately has an encoder tag → authentic (only Remux claims are stream-copy)
    probe = {
        "quality": "WEBDL-1080p",
        "encoder": "x264",
        "keyframes": [],
        "window_s": 40,
    }
    assert rl.is_authentic(probe, POLICY)


def _rec(ep, state, **kw):
    r = {
        "episodeId": ep,
        "seriesId": 96,
        "series": "S",
        "epLabel": "S02E%02d" % ep,
        "fakeFileId": 2200 + ep,
        "fakePath": "f",
        "evidence": "e",
        "state": state,
        "attempts": 0,
        "firstSeen": ep,
        "lastAction": 0,
        "reason": "",
    }
    r.update(kw)
    return r


def test_plan_searches_oldest_first_capped():
    ledger = {str(e): _rec(e, "detected", firstSeen=e) for e in (13, 14, 15)}
    acts = rl.plan_searches(ledger, POLICY)  # searches_per_tick=2
    assert [a["episodeId"] for a in acts] == [13, 14]


def test_plan_searches_concurrent_cap_blocks_when_full():
    ledger = {
        "13": _rec(13, "detected", firstSeen=13),
        "20": _rec(20, "grabbed", firstSeen=1),
        "21": _rec(21, "verifying", firstSeen=2),
    }
    pol = dict(POLICY, max_concurrent_replacements=2)
    acts = rl.plan_searches(ledger, pol)
    assert acts == []


def test_plan_searches_concurrent_cap_leaves_one_slot():
    ledger = {
        "13": _rec(13, "detected", firstSeen=13),
        "14": _rec(14, "detected", firstSeen=14),
        "20": _rec(20, "importing", firstSeen=1),
    }
    pol = dict(POLICY, max_concurrent_replacements=2)
    acts = rl.plan_searches(ledger, pol)
    assert [a["episodeId"] for a in acts] == [13]


def test_advance_grabbed_complete_and_authentic_deletes_then_imports():
    led = {"13": _rec(13, "grabbed", chosen={"quality": "WEBDL-1080p"})}
    new, acts = rl.advance(
        led,
        {"13": {"sizeleft": 0, "id": 555}},
        {"13": 2213},
        {"13": {"quality": "WEBDL-1080p", "encoder": "x264", "keyframes": []}},
        POLICY,
        now=100,
    )
    assert new["13"]["state"] == "importing"
    types = [a["type"] for a in acts]
    assert types == ["delete_file", "import"]
    assert acts[0]["fileId"] == 2213


def test_advance_never_deletes_before_download_complete():
    led = {"13": _rec(13, "grabbed", chosen={"quality": "WEBDL-1080p"})}
    new, acts = rl.advance(
        led, {"13": {"sizeleft": 500_000, "id": 555}}, {"13": 2213}, {}, POLICY, now=100
    )
    assert new["13"]["state"] == "grabbed"
    assert acts == []  # no delete while downloading


def test_advance_grabbed_not_yet_in_queue_stays_within_grab_grace():
    # advance runs the same tick as the grab; the download isn't in Sonarr's queue yet. Within the
    # grab grace the entry stays grabbed (not falsely reset -> which would re-grab every tick).
    led = {"13": _rec(13, "grabbed", lastAction=100, chosen={"quality": "WEBDL-1080p"})}
    new, acts = rl.advance(
        led, {}, {"13": 2213}, {}, POLICY, now=200
    )  # age 100 < 300 grace
    assert new["13"]["state"] == "grabbed"
    assert new["13"]["attempts"] == 0
    assert acts == []


def test_advance_grabbed_lost_after_grace_resets_to_detected():
    led = {"13": _rec(13, "grabbed", lastAction=0, chosen={"quality": "WEBDL-1080p"})}
    new, _ = rl.advance(
        led, {}, {"13": 2213}, {}, POLICY, now=1000
    )  # age 1000 > 300 grace, gone
    assert new["13"]["state"] == "detected"
    assert new["13"]["attempts"] == 1


def test_advance_grabbed_gone_from_queue_but_imported_is_replaced():
    # left the queue because Sonarr already imported it (new fileId != fake) -> replaced, not lost
    led = {"13": _rec(13, "grabbed", lastAction=0, chosen={"quality": "WEBDL-2160p"})}
    new, _ = rl.advance(
        led, {}, {"13": 9999}, {}, POLICY, now=1000
    )  # past grace, new file present
    assert new["13"]["state"] == "replaced"
    assert new["13"]["attempts"] == 0


def test_advance_fake_replacement_blocklists_and_retries_original_untouched():
    led = {
        "13": _rec(
            13,
            "grabbed",
            attempts=0,
            chosen={"quality": "Bluray-1080p Remux"},
        )
    }
    new, acts = rl.advance(
        led,
        {"13": {"sizeleft": 0, "id": 555}},
        {"13": 2213},
        {"13": {"quality": "Bluray-1080p Remux", "encoder": "x265", "keyframes": []}},
        POLICY,
        now=100,
    )
    assert new["13"]["state"] == "detected"  # retry with next candidate
    assert new["13"]["attempts"] == 1
    assert [a["type"] for a in acts] == ["blocklist"]
    assert acts[0]["queueId"] == 555
    assert all(a["type"] != "delete_file" for a in acts)  # original fake untouched


def test_advance_importing_to_replaced_when_new_file_present():
    led = {
        "13": _rec(
            13,
            "importing",
            fakeFileId=2213,
            chosen={"quality": "WEBDL-1080p"},
        )
    }
    new, _ = rl.advance(
        led, {}, {"13": 9999}, {}, POLICY, now=100
    )  # new fileId != fake
    assert new["13"]["state"] == "replaced"


def test_advance_stall_holds():
    led = {
        "13": _rec(
            13,
            "grabbed",
            lastAction=0,
            chosen={"quality": "WEBDL-1080p"},
        )
    }
    pol = dict(POLICY, download_stall_hours=1)
    new, _ = rl.advance(
        led, {"13": {"sizeleft": 500, "id": 555}}, {"13": 2213}, {}, pol, now=3600 + 1
    )
    assert new["13"]["state"] == "held"


def test_advance_idempotent_on_own_output_no_double_action():
    led = {"13": _rec(13, "grabbed", chosen={"quality": "WEBDL-1080p"})}
    q = {"13": {"sizeleft": 0, "id": 555}}
    p = {"13": {"quality": "WEBDL-1080p", "encoder": "x264", "keyframes": []}}
    led2, acts1 = rl.advance(led, q, {"13": 2213}, p, POLICY, now=100)
    assert [a["type"] for a in acts1] == ["delete_file", "import"]
    led3, acts2 = rl.advance(
        led2, {}, {"13": 9999}, {}, POLICY, now=200
    )  # new file imported
    assert acts2 == []
    assert led3["13"]["state"] == "replaced"


def test_advance_verifying_times_out_to_held_when_no_probe():
    led = {
        "13": _rec(
            13,
            "verifying",
            lastAction=0,
            chosen={"quality": "WEBDL-1080p"},
        )
    }
    pol = dict(POLICY, download_stall_hours=1)
    new, _ = rl.advance(
        led, {"13": {"sizeleft": 0, "id": 555}}, {"13": 2213}, {}, pol, now=3601
    )
    assert new["13"]["state"] == "held"


def test_advance_verifying_fake_at_attempt_cap_holds():
    led = {
        "13": _rec(
            13,
            "verifying",
            attempts=2,
            chosen={"quality": "Bluray-1080p Remux"},
        )
    }
    pol = dict(POLICY, max_attempts=3)
    new, acts = rl.advance(
        led,
        {"13": {"sizeleft": 0, "id": 555}},
        {"13": 2213},
        {"13": {"quality": "Bluray-1080p Remux", "encoder": "x265", "keyframes": []}},
        pol,
        now=100,
    )
    assert new["13"]["state"] == "held"
    assert [a["type"] for a in acts] == ["blocklist"]
    assert acts[0]["queueId"] == 555


def test_prune_history_drops_old_replaced_keeps_recent_and_held():
    now = 1_000_000
    ledger = {
        "13": _rec(13, "replaced", lastAction=now - 20 * 86400),  # past 14-day window
        "14": _rec(14, "replaced", lastAction=now - 1 * 86400),  # recent, kept
        "15": _rec(15, "held", lastAction=now - 20 * 86400),  # held, always kept
    }
    pruned = rl.prune_history(ledger, 14, now)
    assert set(pruned) == {"14", "15"}


def test_summarize_counts_states():
    led = {
        "1": {"state": "replaced"},
        "2": {"state": "grabbed"},
        "3": {"state": "held"},
    }
    ok, msg = sh._summarize(led)
    assert ok is False and "held" in msg


def test_release_search_uses_long_search_timeout():
    # the interactive /release search takes minutes; it must NOT use the 15s HTTP_TIMEOUT
    s = sh.Sonarr("http://x", "k", 15)
    s.search_timeout = 999
    seen = {}

    def fake_request(path, method="GET", data=None, timeout=None):
        seen["timeout"] = timeout
        return []

    s._request = fake_request
    s.release_search(17666)
    assert seen["timeout"] == 999


def test_host_path_translates_leading_data_only():
    # Sonarr's /data view -> the host path the reconciler ffprobes (jellyfin can't see /data/torrents)
    assert (
        sh._host_path("/data/torrents/X.mkv", "/srv/containers/data")
        == "/srv/containers/data/torrents/X.mkv"
    )
    assert (
        sh._host_path("/data/torrents/X.mkv", "") == "/data/torrents/X.mkv"
    )  # no root -> unchanged
    assert (
        sh._host_path("/other/X.mkv", "/srv/data") == "/other/X.mkv"
    )  # non-/data -> unchanged


def test_resolve_video_file_folder_and_missing(tmp_path):
    f = tmp_path / "ep.mkv"
    f.write_bytes(b"x" * 100)
    assert sh._resolve_video(str(f)) == str(f)  # a file -> itself
    d = tmp_path / "release"
    d.mkdir()
    (d / "sample.mkv").write_bytes(b"x" * 10)
    big = d / "movie.mkv"
    big.write_bytes(b"x" * 1000)
    (d / "readme.txt").write_bytes(b"notes")
    assert sh._resolve_video(str(d)) == str(big)  # a folder -> the largest video inside
    assert sh._resolve_video(str(tmp_path / "nope")) is None  # missing -> None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert sh._resolve_video(str(empty)) is None  # folder with no video -> None


def test_mode_off_is_noop(tmp_path):
    ok, msg = sh.reconcile_once({"FAKE_REMUX_REPLACE_MODE": "off"})
    assert ok and "off" in msg


class _AssertNoMutationSonarr:
    """Stands in for Sonarr in shadow mode: raises if any mutating endpoint is called."""

    def release_search(self, episode_id):
        return []

    def grab(self, guid, indexer_id):
        raise AssertionError("shadow mode must not grab")

    def delete_episodefile(self, file_id):
        raise AssertionError("shadow mode must not delete")

    def process_downloads(self):
        raise AssertionError("shadow mode must not process downloads")

    def blocklist_queue_item(self, queue_id):
        raise AssertionError("shadow mode must not blocklist")


def test_shadow_mode_performs_zero_sonarr_mutations(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"13": _rec(13, "detected")}))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"search_spacing_s": 0}))
    cfg = {
        "FAKE_REMUX_REPLACE_MODE": "shadow",
        "LEDGER_FILE": str(ledger_path),
        "FAKE_REMUX_POLICY": str(policy_path),
    }
    sh.reconcile_once(cfg, sonarr=_AssertNoMutationSonarr())
    saved = json.loads(ledger_path.read_text())
    assert saved["13"]["state"] != "grabbed"  # never grabbed — shadow mutated nothing


def _qitem(ep, name="WEBDL-1080p", sizeleft=500):
    return {
        "episodeId": ep,
        "id": 500 + ep,
        "title": "Show S02E%02d WEBDL" % ep,
        "quality": {"quality": {"name": name}},
        "sizeleft": sizeleft,
    }


def test_adopt_in_flight_detected_already_downloading_becomes_grabbed():
    led = {"13": _rec(13, "detected")}
    new, adopted = rl.adopt_in_flight(led, {"13": _qitem(13)}, now=100)
    assert adopted == 1
    assert new["13"]["state"] == "grabbed"
    assert new["13"]["chosen"]["quality"] == "WEBDL-1080p"
    assert new["13"]["lastAction"] == 100
    # the seeded fields the reconciler needs for a later verify + delete survive adoption
    assert new["13"]["fakeFileId"] == _rec(13, "detected")["fakeFileId"]
    assert new["13"]["seriesId"] == 96


def test_adopt_in_flight_detected_not_in_queue_unchanged():
    led = {"13": _rec(13, "detected")}
    new, adopted = rl.adopt_in_flight(led, {}, now=100)
    assert adopted == 0
    assert new["13"]["state"] == "detected"


def test_adopt_in_flight_only_touches_detected_entries():
    led = {"13": _rec(13, "grabbed"), "14": _rec(14, "importing")}
    new, adopted = rl.adopt_in_flight(
        led, {"13": _qitem(13), "14": _qitem(14)}, now=100
    )
    assert adopted == 0
    assert new["13"]["state"] == "grabbed" and new["14"]["state"] == "importing"


def test_files_for_ledger_covers_grabbed_not_just_importing():
    # regression (M2): a 'grabbed' series must be queried so advance()'s auto-import success path can
    # see the new fileId; scoping to 'importing' only left that path dead in production.
    led = {
        "13": _rec(13, "grabbed", seriesId=96),
        "14": _rec(14, "detected", seriesId=97),
    }
    called = []

    class _StubSonarr:
        def episodefile_by_episode(self, series_id):
            called.append(series_id)
            return {"13": 9999} if series_id == 96 else {}

    files = sh._files_for_ledger(_StubSonarr(), led)
    assert called == [96]  # grabbed series queried; detected series (97) is not
    assert files == {"13": 9999}


class _AdoptSonarr:
    """Live-mode stub where episode 13 is already downloading — searching/grabbing it again would be
    the duplicate the idempotency guard prevents."""

    def __init__(self):
        self.searched = []
        self.grabbed = []

    def queue(self):
        return [_qitem(13)]

    def release_search(self, episode_id):
        self.searched.append(episode_id)
        return []

    def grab(self, guid, indexer_id):
        self.grabbed.append(guid)
        return {"id": 1}

    def episodefile_by_episode(self, series_id):
        return {}


def test_reconcile_adopts_in_flight_instead_of_regrabbing(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"13": _rec(13, "detected")}))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"search_spacing_s": 0}))
    cfg = {
        "FAKE_REMUX_REPLACE_MODE": "live",
        "LEDGER_FILE": str(ledger_path),
        "FAKE_REMUX_POLICY": str(policy_path),
    }
    s = _AdoptSonarr()
    sh.reconcile_once(cfg, sonarr=s)
    assert s.searched == []  # never searched — the in-flight download was adopted
    assert s.grabbed == []  # and never re-grabbed
    assert json.loads(ledger_path.read_text())["13"]["state"] == "grabbed"


def test_trim_outcomes_bounds_a_grown_log(tmp_path, monkeypatch):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("".join("line %d\n" % i for i in range(5000)))
    monkeypatch.setattr(sh, "_OUTCOMES_MAX_BYTES", 1)  # force the size gate
    monkeypatch.setattr(sh, "_OUTCOMES_KEEP_LINES", 100)
    sh._trim_outcomes(str(path))
    lines = path.read_text().splitlines()
    assert len(lines) == 100 and lines[-1] == "line 4999"
