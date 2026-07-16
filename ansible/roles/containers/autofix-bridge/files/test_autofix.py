import importlib.util
import pathlib

# Load the bind-mounted script directly (not a package), mirroring monitor-bridge/test_check.py.
_SPEC = importlib.util.spec_from_file_location(
    "autofix", pathlib.Path(__file__).with_name("autofix.py")
)
autofix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(autofix)

PATTERNS = ["executable file with extension", "potentially dangerous", "sample"]
CLIENT_PATTERNS = [
    "unable to communicate",
    "not responding",
    "failed to connect",
    "connection refused",
    "download client is unavailable",
]


def _item(
    status=None,
    state=None,
    messages=None,
    download_id="d1",
    qid=1,
    series_id=None,
    movie_id=None,
    title="Some.Release",
    error_message=None,
):
    it = {
        "trackedDownloadStatus": status,
        "trackedDownloadState": state,
        "downloadId": download_id,
        "id": qid,
        "title": title,
    }
    if messages is not None:
        it["statusMessages"] = [{"title": title, "messages": messages}]
    if series_id is not None:
        it["seriesId"] = series_id
    if movie_id is not None:
        it["movieId"] = movie_id
    if error_message is not None:
        it["errorMessage"] = error_message
    return it


# --- dangerous ---------------------------------------------------------------
def test_dangerous_matches_executable_message_case_insensitively():
    msgs = ["Caution: Found EXECUTABLE File With Extension: '.exe'"]
    assert autofix.dangerous(msgs, PATTERNS) is True


def test_dangerous_ignores_benign_message():
    assert autofix.dangerous(["Waiting to import"], PATTERNS) is False


def test_dangerous_empty_is_false():
    assert autofix.dangerous([], PATTERNS) is False


def test_dangerous_matches_mixed_case_pattern():
    # patterns aren't pre-lowered by the caller here -> dangerous() must lower them itself
    assert autofix.dangerous(["found a Sample file"], ["SaMpLe"]) is True


# --- is_candidate ------------------------------------------------------------
def test_candidate_hard_bad_status_error():
    assert autofix.is_candidate(_item(status="error"), PATTERNS) is True


def test_candidate_hard_bad_state_import_blocked():
    assert autofix.is_candidate(_item(state="importBlocked"), PATTERNS) is True


def test_candidate_hard_bad_state_import_failed():
    assert autofix.is_candidate(_item(state="importFailed"), PATTERNS) is True


def test_plain_warning_is_not_a_candidate():
    # warning with no dangerous message -> notify-only (monitor-bridge pages it), not auto-blocked
    assert (
        autofix.is_candidate(
            _item(status="warning", messages=["Waiting to import"]), PATTERNS
        )
        is False
    )


def test_warning_with_dangerous_message_is_candidate():
    # the 2026-07-01 poisoned-.exe class
    assert (
        autofix.is_candidate(
            _item(
                status="warning",
                messages=["Caution: Found executable file with extension: '.exe'"],
            ),
            PATTERNS,
        )
        is True
    )


def test_import_pending_with_messages_is_not_a_candidate():
    assert (
        autofix.is_candidate(
            _item(
                state="importPending", messages=["Not an upgrade for existing episode"]
            ),
            PATTERNS,
        )
        is False
    )


# --- client_comm_error / bare-error exclusion --------------------------------
def test_client_comm_error_helper_checks_both_sources():
    in_status = _item(
        status="error", messages=["Unable to communicate with qBittorrent."]
    )
    assert autofix.client_comm_error(in_status, CLIENT_PATTERNS) is True

    in_error_message = _item(
        status="error", error_message="qBittorrent is not responding"
    )
    assert autofix.client_comm_error(in_error_message, CLIENT_PATTERNS) is True


def test_error_with_client_comm_statusmessage_excluded():
    item = _item(status="error", messages=["Unable to communicate with qBittorrent."])
    assert autofix.is_candidate(item, PATTERNS, CLIENT_PATTERNS) is False


def test_error_with_client_comm_in_errormessage_excluded():
    item = _item(status="error", error_message="qBittorrent is not responding")
    assert autofix.is_candidate(item, PATTERNS, CLIENT_PATTERNS) is False


def test_error_without_client_message_still_candidate():
    item = _item(status="error", messages=["Waiting to import"])
    assert autofix.is_candidate(item, PATTERNS, CLIENT_PATTERNS) is True


def test_import_blocked_with_client_message_still_candidate():
    # import-step failure wins; the exclusion only guards a bare `error` status
    item = _item(
        state="importBlocked", messages=["Unable to communicate with qBittorrent."]
    )
    assert autofix.is_candidate(item, PATTERNS, CLIENT_PATTERNS) is True


def test_malware_signature_still_candidate_with_client_patterns():
    item = _item(
        status="warning",
        messages=["Caution: Found executable file with extension: '.exe'"],
    )
    assert autofix.is_candidate(item, PATTERNS, CLIENT_PATTERNS) is True


# --- eligible (grace + blast radius) -----------------------------------------
def test_not_eligible_until_grace_met():
    streaks = {}
    assert autofix.eligible({"a"}, streaks, grace=3, max_actions=5) == ([], [])
    assert autofix.eligible({"a"}, streaks, grace=3, max_actions=5) == ([], [])
    assert autofix.eligible({"a"}, streaks, grace=3, max_actions=5) == (["a"], [])
    assert streaks["a"] == 3


def test_streak_resets_when_candidate_clears():
    streaks = {}
    autofix.eligible({"a"}, streaks, grace=3, max_actions=5)  # a=1
    autofix.eligible(set(), streaks, grace=3, max_actions=5)  # a cleared
    assert "a" not in streaks
    to_act, _ = autofix.eligible({"a"}, streaks, grace=3, max_actions=5)  # a=1 again
    assert to_act == []


def test_blast_radius_holds_and_acts_on_none():
    # 6 items all past grace, cap 5 -> act on none, hold all
    streaks = {k: 3 for k in "abcdef"}
    to_act, held = autofix.eligible(set("abcdef"), streaks, grace=3, max_actions=5)
    assert to_act == []
    assert held == sorted("abcdef")


def test_within_cap_all_act():
    streaks = {k: 3 for k in "abc"}
    to_act, held = autofix.eligible(set("abc"), streaks, grace=3, max_actions=5)
    assert to_act == sorted("abc")
    assert held == []


def test_exactly_at_cap_all_act():
    # 5 grace-met items, cap 5 -> all 5 act, none held (pins the strict `>` boundary)
    streaks = {k: 3 for k in "abcde"}
    to_act, held = autofix.eligible(set("abcde"), streaks, grace=3, max_actions=5)
    assert to_act == sorted("abcde")
    assert held == []


# --- item_key ------------------------------------------------------------------
def test_item_key_stable_across_repeated_calls():
    it = _item(download_id="hash123", qid=7)
    assert autofix.item_key("Sonarr", it) == autofix.item_key("Sonarr", it)


def test_item_key_uses_download_id_when_present():
    it = _item(download_id="hash123", qid=7)
    assert autofix.item_key("Sonarr", it) == "Sonarr:dl:hash123"


def test_item_key_falls_back_to_queue_id_without_download_id():
    it = _item(download_id=None, qid=7)
    assert autofix.item_key("Sonarr", it) == "Sonarr:id:7"


def test_item_key_distinct_across_apps_for_same_id():
    # regression test for the cross-app streak-key collision fix
    sonarr_item = _item(download_id=None, qid=7)
    radarr_item = _item(download_id=None, qid=7)
    assert autofix.item_key("Sonarr", sonarr_item) != autofix.item_key(
        "Radarr", radarr_item
    )


def test_item_key_numeric_download_id_does_not_collide_with_id_fallback():
    # a numeric-string downloadId (e.g. a future NZBGet numeric id) must not alias the
    # same-numbered queue `id` fallback of another item
    dl_item = _item(download_id="7", qid=99)
    id_item = _item(download_id=None, qid=7)
    assert autofix.item_key("Sonarr", dl_item) == "Sonarr:dl:7"
    assert autofix.item_key("Sonarr", id_item) == "Sonarr:id:7"
    assert autofix.item_key("Sonarr", dl_item) != autofix.item_key("Sonarr", id_item)


# --- search_command ----------------------------------------------------------
def test_search_command_sonarr_series_level():
    cmd = autofix.search_command("Sonarr", _item(series_id=42))
    assert cmd == {"name": "SeriesSearch", "seriesId": 42}


def test_search_command_radarr_movie_level():
    cmd = autofix.search_command("Radarr", _item(movie_id=7))
    assert cmd == {"name": "MoviesSearch", "movieIds": [7]}


def test_search_command_missing_id_returns_none():
    assert autofix.search_command("Sonarr", _item()) is None


# --- item_reason / format_action ---------------------------------------------
def test_item_reason_prefers_status_messages():
    it = _item(
        status="warning", messages=["Found executable file with extension: '.exe'"]
    )
    assert "executable" in autofix.item_reason(it)


def test_item_reason_falls_back_to_status():
    assert autofix.item_reason(_item(status="error")) == "error"


def test_format_action_dry_run_says_would():
    s = autofix.format_action(True, "Sonarr", "Bad.Release", "importBlocked", 3, 3)
    assert s.startswith("WOULD blocklist [Sonarr]")
    assert "(3/3)" in s


def test_format_action_live_says_blocklisted():
    s = autofix.format_action(False, "Radarr", "Bad.Movie", "error", 3, 3)
    assert s.startswith("Blocklisted + re-searched [Radarr]")


def test_sanitize_defuses_discord_mentions_and_backticks():
    assert "@" not in autofix.sanitize("@everyone `rm`")


# --- _dry_run_enabled (fail-safe parsing) -------------------------------------
def test_dry_run_enabled_true_for_explicit_true_values():
    assert autofix._dry_run_enabled("true") is True
    assert autofix._dry_run_enabled("1") is True


def test_dry_run_enabled_true_for_typo_value():
    # fail SAFE: an unrecognized/typo'd value must not fall through to live mode
    assert autofix._dry_run_enabled("ture") is True


def test_dry_run_enabled_true_for_empty_value():
    assert autofix._dry_run_enabled("") is True


def test_dry_run_enabled_false_for_explicit_disable_values():
    assert autofix._dry_run_enabled("false") is False
    assert autofix._dry_run_enabled("0") is False
    assert autofix._dry_run_enabled("no") is False
    assert autofix._dry_run_enabled("FALSE") is False


# --- run_once (I/O-mocked integration) ----------------------------------------
def _configure_sonarr_only(monkeypatch):
    monkeypatch.setattr(autofix, "post_discord", lambda msg: None)
    monkeypatch.setattr(autofix, "push", lambda ok, msg: None)
    monkeypatch.setattr(autofix, "SONARR_API_KEY", "sonarr-key")
    monkeypatch.setattr(autofix, "RADARR_API_KEY", "")


def _fake_request(records, calls):
    """A `_request` stand-in: GETs (the queue poll) return the canned records; anything
    else (DELETE / /api/v3/command) is recorded instead of hitting the network."""

    def fake(url, method="GET", headers=None, data=None):
        if method == "GET":
            return {"records": records}
        calls.append((method, url, data))
        return None

    return fake


def test_run_once_dry_run_makes_zero_mutating_calls(monkeypatch):
    item = _item(status="error", download_id=None, qid=7, series_id=42)
    calls = []
    monkeypatch.setattr(autofix, "_request", _fake_request([item], calls))
    _configure_sonarr_only(monkeypatch)
    monkeypatch.setattr(autofix, "DRY_RUN", True)

    key = autofix.item_key("Sonarr", item)
    streaks = {key: autofix.GRACE_CYCLES - 1}
    ok, msg = autofix.run_once(streaks)

    assert calls == []
    assert ok is True


def test_run_once_live_deletes_then_searches_in_order(monkeypatch):
    item = _item(status="error", download_id=None, qid=7, series_id=42)
    calls = []
    monkeypatch.setattr(autofix, "_request", _fake_request([item], calls))
    _configure_sonarr_only(monkeypatch)
    monkeypatch.setattr(autofix, "DRY_RUN", False)

    key = autofix.item_key("Sonarr", item)
    streaks = {key: autofix.GRACE_CYCLES - 1}
    ok, msg = autofix.run_once(streaks)

    assert ok is True
    assert len(calls) == 2
    method, url, data = calls[0]
    assert method == "DELETE"
    assert "removeFromClient=true&blocklist=true" in url
    method, url, data = calls[1]
    assert method == "POST"
    assert url.endswith("/api/v3/command")
    assert data == {"name": "SeriesSearch", "seriesId": 42}


def test_run_once_held_branch_makes_zero_mutating_calls(monkeypatch):
    n = autofix.MAX_ACTIONS_PER_CYCLE + 1
    items = [
        _item(status="error", download_id="dl-%d" % i, qid=i, series_id=100 + i)
        for i in range(n)
    ]
    calls = []
    monkeypatch.setattr(autofix, "_request", _fake_request(items, calls))
    _configure_sonarr_only(monkeypatch)
    monkeypatch.setattr(autofix, "DRY_RUN", False)

    streaks = {autofix.item_key("Sonarr", it): autofix.GRACE_CYCLES - 1 for it in items}
    ok, msg = autofix.run_once(streaks)

    assert calls == []
    assert ok is False
    assert "holding" in msg


def test_run_once_no_api_keys_is_disabled_with_no_requests(monkeypatch):
    def fake(url, method="GET", headers=None, data=None):
        raise AssertionError("no HTTP call should happen with no API keys configured")

    monkeypatch.setattr(autofix, "_request", fake)
    monkeypatch.setattr(autofix, "SONARR_API_KEY", "")
    monkeypatch.setattr(autofix, "RADARR_API_KEY", "")

    ok, msg = autofix.run_once({})

    assert (ok, msg) == (True, "arr auto-block disabled (no API keys)")


# --- is_fake_remux / resolution_height / fake_files --------------------------
def test_resolution_height_parses_and_fails_safe():
    assert autofix.resolution_height("1920x1080") == 1080
    assert autofix.resolution_height("3840x2160") == 2160
    assert autofix.resolution_height(None) is None
    assert autofix.resolution_height("garbage") is None


def test_fake_remux_1080p_or_720p_hevc_is_flagged():
    assert autofix.is_fake_remux("Bluray-1080p Remux", "1920x1080", "h265") is True
    assert autofix.is_fake_remux("Bluray-1080p Remux", "1920x1080", "x265") is True
    assert autofix.is_fake_remux("Bluray-720p Remux", "1280x720", "hevc") is True


def test_real_remux_and_web_hevc_are_not_flagged():
    # a genuine <=1080p remux is the untouched AVC disc stream
    assert autofix.is_fake_remux("Bluray-1080p Remux", "1920x1080", "h264") is False
    # HEVC in a NON-remux quality is normal (WEB/BD encodes) — must not trip
    assert autofix.is_fake_remux("WEBDL-1080p", "1920x1080", "h265") is False


def test_2160p_hevc_remux_is_legitimate():
    # UHD remuxes ARE HEVC — the resolution gate must exclude them
    assert autofix.is_fake_remux("Bluray-2160p Remux", "3840x2160", "h265") is False


def test_unknown_resolution_or_codec_fails_safe():
    assert autofix.is_fake_remux("Bluray-1080p Remux", None, "h265") is False
    assert autofix.is_fake_remux("Bluray-1080p Remux", "1920x1080", None) is False


def _episodefile(quality, resolution, codec, fid=1, series_id=9, path="S01E01.mkv"):
    return {
        "id": fid,
        "seriesId": series_id,
        "relativePath": path,
        "quality": {"quality": {"name": quality}},
        "mediaInfo": {"resolution": resolution, "videoCodec": codec},
    }


def test_fake_files_selects_only_the_fake_with_flat_fields():
    files = [
        _episodefile(
            "Bluray-1080p Remux", "1920x1080", "h265", fid=11, path="fake.mkv"
        ),
        _episodefile("WEBDL-1080p", "1920x1080", "h265", fid=12, path="ok.mkv"),
        _episodefile(
            "Bluray-1080p Remux", "1920x1080", "h264", fid=13, path="realremux.mkv"
        ),
    ]
    out = autofix.fake_files(files, "Mushoku Tensei")
    assert len(out) == 1
    assert out[0]["fileId"] == 11
    assert out[0]["seriesId"] == 9
    assert out[0]["seriesTitle"] == "Mushoku Tensei"
    assert out[0]["codec"] == "h265"


# --- run_fake_remux_scan (I/O-mocked) ----------------------------------------
def _fake_scan_request(series, efs_by_series, calls):
    """A `_request` stand-in: GET /series returns the list, GET /episodefile?seriesId=N returns
    that series' files; DELETE / command are recorded instead of hitting the network."""

    def fake(url, method="GET", headers=None, data=None):
        if method != "GET":
            calls.append((method, url, data))
            return None
        if url.endswith("/api/v3/series"):
            return series
        return efs_by_series.get(int(url.rsplit("seriesId=", 1)[1]), [])

    return fake


def test_fake_remux_scan_dry_run_makes_zero_mutations(monkeypatch):
    series = [{"id": 9, "title": "Mushoku"}]
    efs = {9: [_episodefile("Bluray-1080p Remux", "1920x1080", "h265", fid=11)]}
    calls = []
    monkeypatch.setattr(autofix, "_request", _fake_scan_request(series, efs, calls))
    monkeypatch.setattr(autofix, "post_discord", lambda msg: None)
    monkeypatch.setattr(autofix, "SONARR_API_KEY", "k")
    monkeypatch.setattr(autofix, "FAKEREMUX_DRY_RUN", True)

    summary = autofix.run_fake_remux_scan()

    assert calls == []
    assert "1 fake remux" in summary


def test_fake_remux_scan_live_deletes_fake_then_searches(monkeypatch):
    series = [{"id": 9, "title": "Mushoku"}]
    efs = {
        9: [
            _episodefile("Bluray-1080p Remux", "1920x1080", "h265", fid=11),
            _episodefile("WEBDL-1080p", "1920x1080", "h265", fid=12),
        ]
    }
    calls = []
    monkeypatch.setattr(autofix, "_request", _fake_scan_request(series, efs, calls))
    monkeypatch.setattr(autofix, "post_discord", lambda msg: None)
    monkeypatch.setattr(autofix, "SONARR_API_KEY", "k")
    monkeypatch.setattr(autofix, "FAKEREMUX_DRY_RUN", False)

    autofix.run_fake_remux_scan()

    # only the fake (fid 11) is deleted, then one SeriesSearch; the WEB file is untouched
    assert [c[0] for c in calls] == ["DELETE", "POST"]
    assert "/api/v3/episodefile/11" in calls[0][1]
    assert calls[1][2] == {"name": "SeriesSearch", "seriesId": 9}


def test_fake_remux_scan_mass_match_holds_and_acts_on_none(monkeypatch):
    n = autofix.FAKEREMUX_MAX_PER_SCAN + 1
    series = [{"id": i, "title": "S%d" % i} for i in range(n)]
    efs = {
        i: [
            _episodefile(
                "Bluray-1080p Remux", "1920x1080", "h265", fid=100 + i, series_id=i
            )
        ]
        for i in range(n)
    }
    calls = []
    monkeypatch.setattr(autofix, "_request", _fake_scan_request(series, efs, calls))
    monkeypatch.setattr(autofix, "post_discord", lambda msg: None)
    monkeypatch.setattr(autofix, "SONARR_API_KEY", "k")
    monkeypatch.setattr(autofix, "FAKEREMUX_DRY_RUN", False)

    summary = autofix.run_fake_remux_scan()

    assert calls == []
    assert "holding" in summary
