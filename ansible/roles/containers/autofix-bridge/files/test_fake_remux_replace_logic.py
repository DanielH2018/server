import importlib.util
import pathlib

# Load the host script's pure core directly (not a package), mirroring test_fake_remux_logic.py.
_SPEC = importlib.util.spec_from_file_location(
    "fake_remux_replace_logic",
    pathlib.Path(__file__).with_name("fake_remux_replace_logic.py"),
)
rl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rl)

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
