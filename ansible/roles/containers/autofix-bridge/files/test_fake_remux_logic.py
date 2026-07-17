import importlib.util
import pathlib

# Load the host script's pure core directly (not a package), mirroring test_autofix.py.
_SPEC = importlib.util.spec_from_file_location(
    "fake_remux_logic", pathlib.Path(__file__).with_name("fake_remux_logic.py")
)
frl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(frl)

MARKERS = frl.DEFAULT_RE_ENCODER_MARKERS
# The real NTRX file (2026-07-16): a "Bluray-1080p Remux" that is actually a 10.4 s-GOP hevc_qsv encode.
NTRX_ENCODER = "Lavc61.19.101 hevc_qsv"
NTRX_KEYFRAMES = [0.0, 10.427, 20.854, 31.281]


# --- encoder_is_reencoder ----------------------------------------------------
def test_encoder_flags_the_ntrx_reencode():
    assert frl.encoder_is_reencoder(NTRX_ENCODER, MARKERS) is True


def test_encoder_flags_cli_and_hardware_encoders():
    for tag in ("x265", "libx264", "hevc_nvenc [info]", "HANDBRAKE 1.7"):
        assert frl.encoder_is_reencoder(tag, MARKERS) is True


def test_encoder_empty_or_disc_tag_is_not_a_reencoder():
    assert frl.encoder_is_reencoder(None, MARKERS) is False
    assert frl.encoder_is_reencoder("", MARKERS) is False
    # a professional disc-mastering tool name isn't in the marker set
    assert frl.encoder_is_reencoder("Sony BVE HDCAM", MARKERS) is False


# --- max_keyframe_gap / gop_exceeds ------------------------------------------
def test_gap_is_max_consecutive_keyframe_distance():
    assert frl.max_keyframe_gap(NTRX_KEYFRAMES, 40) == 10.427


def test_short_gop_remux_is_within_threshold():
    # a real remux keyframes ~every 2 s
    assert frl.gop_exceeds([0.0, 2.0, 4.0, 6.0], 40, 5) is False


def test_long_gop_reencode_exceeds_threshold():
    assert frl.gop_exceeds(NTRX_KEYFRAMES, 40, 5) is True


def test_single_keyframe_in_window_means_gop_at_least_window():
    # only the opening keyframe within a 40 s read -> GOP >= 40 s > threshold
    assert frl.max_keyframe_gap([0.0], 40) == 40.0
    assert frl.gop_exceeds([0.0], 40, 5) is True


# --- reencode_evidence -------------------------------------------------------
def test_evidence_prefers_encoder_tag_over_gop():
    ev = frl.reencode_evidence(
        "Bluray-1080p Remux", NTRX_ENCODER, NTRX_KEYFRAMES, 40, 5, MARKERS
    )
    assert ev.startswith("encoder=")


def test_evidence_falls_back_to_gop_when_tag_absent():
    ev = frl.reencode_evidence(
        "Bluray-1080p Remux", None, NTRX_KEYFRAMES, 40, 5, MARKERS
    )
    assert ev == "GOP=10.4s"


def test_genuine_remux_has_no_evidence():
    # short GOP + no re-encoder tag = untouched disc stream
    assert (
        frl.reencode_evidence(
            "Bluray-1080p Remux", None, [0.0, 2.0, 4.0], 40, 5, MARKERS
        )
        is None
    )


def test_non_remux_quality_is_never_evidence_even_with_long_gop():
    # streaming/WEB encodes legitimately have long GOPs + Lavc tags — must not trip
    assert (
        frl.reencode_evidence(
            "WEBDL-1080p", NTRX_ENCODER, NTRX_KEYFRAMES, 40, 5, MARKERS
        )
        is None
    )


def test_2160p_reencode_labeled_remux_is_still_caught():
    # generalization win over the old codec heuristic: UHD isn't excluded — a real 2160p remux has a
    # short GOP + no re-encoder tag, so only a genuine re-encode trips regardless of resolution
    ev = frl.reencode_evidence("Bluray-2160p Remux", "x265", [0.0], 40, 5, MARKERS)
    assert ev is not None


# --- remux_candidates --------------------------------------------------------
def _episodefile(
    quality, codec, fid=1, series_id=9, path="/data/media/tv/S/S01E01.mkv"
):
    return {
        "id": fid,
        "seriesId": series_id,
        "path": path,
        "relativePath": path.rsplit("/", 1)[-1],
        "quality": {"quality": {"name": quality}},
        "mediaInfo": {"videoCodec": codec},
    }


def test_remux_candidates_selects_only_remux_qualities_with_a_path():
    files = [
        _episodefile("Bluray-1080p Remux", "hevc", fid=11),
        _episodefile("WEBDL-1080p", "h264", fid=12),
        _episodefile("Bluray-2160p Remux", "hevc", fid=13),
    ]
    # a remux row with no path can't be probed -> dropped
    files.append({"id": 14, "quality": {"quality": {"name": "Bluray-1080p Remux"}}})
    out = frl.remux_candidates(files, "Mushoku Tensei")
    ids = sorted(c["fileId"] for c in out)
    assert ids == [11, 13]
    assert out[0]["seriesTitle"] == "Mushoku Tensei"
    assert out[0]["path"].startswith("/data/media/tv/")


# --- select_fakes ------------------------------------------------------------
def _probed(
    quality="Bluray-1080p Remux", encoder=None, keyframes=None, fid=1, series_id=9
):
    return {
        "fileId": fid,
        "seriesId": series_id,
        "seriesTitle": "S",
        "path": "/data/media/tv/S/e.mkv",
        "relativePath": "e.mkv",
        "quality": quality,
        "codec": "hevc",
        "encoder": encoder,
        "keyframes": keyframes or [],
    }


def test_select_fakes_keeps_reencodes_and_tags_evidence():
    probed = [
        _probed(encoder=NTRX_ENCODER, keyframes=NTRX_KEYFRAMES, fid=11),
        _probed(encoder=None, keyframes=[0.0, 2.0, 4.0], fid=12),  # genuine
        _probed(quality="WEBDL-1080p", encoder=NTRX_ENCODER, fid=13),  # not remux
    ]
    fakes = frl.select_fakes(probed, 40, 5, MARKERS)
    assert [f["fileId"] for f in fakes] == [11]
    assert fakes[0]["evidence"].startswith("encoder=")


# --- plan_fake_remux_actions -------------------------------------------------
def _fake(fid=1, series_id=9):
    return {
        "fileId": fid,
        "seriesId": series_id,
        "relativePath": "e%d.mkv" % fid,
        "quality": "Bluray-1080p Remux",
        "evidence": "GOP=10.4s",
    }


def test_plan_empty_is_ok_and_no_actions():
    plan = frl.plan_fake_remux_actions([], dry_run=True, max_per_scan=5)
    assert plan["ok"] is True
    assert plan["deletes"] == [] and plan["searches"] == []
    assert plan["summary"] == "library clean"


def test_plan_dry_run_flags_but_never_mutates_and_pages():
    plan = frl.plan_fake_remux_actions([_fake(11)], dry_run=True, max_per_scan=5)
    assert plan["deletes"] == [] and plan["searches"] == []
    assert plan["ok"] is False  # report-only surfaces the file
    assert "report-only" in plan["summary"]
    assert plan["lines"][0].startswith("WOULD delete+re-search")


def test_plan_live_deletes_files_and_dedupes_series_searches():
    fakes = [_fake(11, series_id=9), _fake(12, series_id=9), _fake(13, series_id=7)]
    plan = frl.plan_fake_remux_actions(fakes, dry_run=False, max_per_scan=5)
    assert plan["deletes"] == [11, 12, 13]
    assert plan["searches"] == [7, 9]  # one search per series, sorted
    assert plan["ok"] is True
    assert plan["lines"][0].startswith("Deleted+re-searched")


def test_plan_mass_match_holds_acts_on_none_and_pages():
    fakes = [_fake(i, series_id=i) for i in range(6)]  # > max_per_scan
    plan = frl.plan_fake_remux_actions(fakes, dry_run=False, max_per_scan=5)
    assert plan["hold"] is True
    assert plan["deletes"] == [] and plan["searches"] == []
    assert plan["ok"] is False
    assert "holding" in plan["summary"]


def test_plan_exactly_at_cap_acts():
    fakes = [_fake(i, series_id=i) for i in range(5)]  # == max_per_scan, not over
    plan = frl.plan_fake_remux_actions(fakes, dry_run=False, max_per_scan=5)
    assert plan["hold"] is False
    assert len(plan["deletes"]) == 5


# --- seed_ledger --------------------------------------------------------------
def test_seed_ledger_adds_new_and_blasts_over_cap():
    fakes = [
        {
            "fileId": i,
            "seriesId": 96,
            "seriesTitle": "S",
            "relativePath": "E%d" % i,
            "evidence": "e",
            "episodeId": i,
        }
        for i in range(3)
    ]
    led, held = frl.seed_ledger({}, fakes, max_concurrent=5, now=1)
    assert not held and len(led) == 3 and led["0"]["state"] == "detected"
    led2, held2 = frl.seed_ledger({}, fakes, max_concurrent=2, now=1)
    assert held2 and led2 == {}  # over cap → seed none


def test_seed_ledger_skips_already_seeded_and_preserves_existing_state():
    existing = {"0": {"state": "verifying", "episodeId": 0}}
    fakes = [
        {
            "fileId": 0,
            "seriesId": 96,
            "seriesTitle": "S",
            "relativePath": "E0",
            "evidence": "e",
            "episodeId": 0,
        },
        {
            "fileId": 1,
            "seriesId": 96,
            "seriesTitle": "S",
            "relativePath": "E1",
            "evidence": "e",
            "episodeId": 1,
        },
    ]
    led, held = frl.seed_ledger(existing, fakes, max_concurrent=5, now=2)
    assert not held
    assert led["0"]["state"] == "verifying"  # untouched
    assert led["1"]["state"] == "detected"  # newly seeded


# --- ffprobe output parsers --------------------------------------------------
def test_parse_encoder_tag_reads_video_stream_encoder():
    # shape of `ffprobe -select_streams v:0 -show_entries stream_tags=ENCODER -of json`
    j = '{"streams":[{"tags":{"ENCODER":"Lavc61.19.101 hevc_qsv"}}]}'
    assert frl.parse_encoder_tag(j) == "Lavc61.19.101 hevc_qsv"


def test_parse_encoder_tag_missing_or_malformed_is_none():
    assert frl.parse_encoder_tag('{"streams":[{"tags":{}}]}') is None
    assert frl.parse_encoder_tag('{"streams":[]}') is None
    assert frl.parse_encoder_tag("not json") is None


def test_parse_keyframe_csv_keeps_only_keyframe_times():
    # `frame=key_frame,pts_time -of csv=p=0`: keep rows flagged 1, drop non-keyframes + junk
    csv = "1,0.000000\n0,0.041000\n1,10.427000\n1,\n,\n1,20.854000\n"
    assert frl.parse_keyframe_csv(csv) == [0.0, 10.427, 20.854]


# --- sanitize ----------------------------------------------------------------
def test_sanitize_defuses_mentions_and_backticks():
    assert "@" not in frl.sanitize("@everyone `rm -rf`")
