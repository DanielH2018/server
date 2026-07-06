import importlib.util
import pathlib

# Load the bind-mounted script directly (not a package), mirroring monitor-bridge/test_check.py.
_SPEC = importlib.util.spec_from_file_location(
    "autoblock", pathlib.Path(__file__).with_name("autoblock.py")
)
autoblock = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(autoblock)

PATTERNS = ["executable file with extension", "potentially dangerous", "sample"]


def _item(
    status=None,
    state=None,
    messages=None,
    download_id="d1",
    qid=1,
    series_id=None,
    movie_id=None,
    title="Some.Release",
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
    return it


# --- dangerous ---------------------------------------------------------------
def test_dangerous_matches_executable_message_case_insensitively():
    msgs = ["Caution: Found EXECUTABLE File With Extension: '.exe'"]
    assert autoblock.dangerous(msgs, PATTERNS) is True


def test_dangerous_ignores_benign_message():
    assert autoblock.dangerous(["Waiting to import"], PATTERNS) is False


def test_dangerous_empty_is_false():
    assert autoblock.dangerous([], PATTERNS) is False


# --- is_candidate ------------------------------------------------------------
def test_candidate_hard_bad_status_error():
    assert autoblock.is_candidate(_item(status="error"), PATTERNS) is True


def test_candidate_hard_bad_state_import_blocked():
    assert autoblock.is_candidate(_item(state="importBlocked"), PATTERNS) is True


def test_candidate_hard_bad_state_import_failed():
    assert autoblock.is_candidate(_item(state="importFailed"), PATTERNS) is True


def test_plain_warning_is_not_a_candidate():
    # warning with no dangerous message -> notify-only (monitor-bridge pages it), not auto-blocked
    assert (
        autoblock.is_candidate(
            _item(status="warning", messages=["Waiting to import"]), PATTERNS
        )
        is False
    )


def test_warning_with_dangerous_message_is_candidate():
    # the 2026-07-01 poisoned-.exe class
    assert (
        autoblock.is_candidate(
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
        autoblock.is_candidate(
            _item(
                state="importPending", messages=["Not an upgrade for existing episode"]
            ),
            PATTERNS,
        )
        is False
    )


# --- eligible (grace + blast radius) -----------------------------------------
def test_not_eligible_until_grace_met():
    streaks = {}
    assert autoblock.eligible({"a"}, streaks, grace=3, max_actions=5) == ([], [])
    assert autoblock.eligible({"a"}, streaks, grace=3, max_actions=5) == ([], [])
    assert autoblock.eligible({"a"}, streaks, grace=3, max_actions=5) == (["a"], [])
    assert streaks["a"] == 3


def test_streak_resets_when_candidate_clears():
    streaks = {}
    autoblock.eligible({"a"}, streaks, grace=3, max_actions=5)  # a=1
    autoblock.eligible(set(), streaks, grace=3, max_actions=5)  # a cleared
    assert "a" not in streaks
    to_act, _ = autoblock.eligible({"a"}, streaks, grace=3, max_actions=5)  # a=1 again
    assert to_act == []


def test_blast_radius_holds_and_acts_on_none():
    # 6 items all past grace, cap 5 -> act on none, hold all
    streaks = {k: 3 for k in "abcdef"}
    to_act, held = autoblock.eligible(set("abcdef"), streaks, grace=3, max_actions=5)
    assert to_act == []
    assert held == sorted("abcdef")


def test_within_cap_all_act():
    streaks = {k: 3 for k in "abc"}
    to_act, held = autoblock.eligible(set("abc"), streaks, grace=3, max_actions=5)
    assert to_act == sorted("abc")
    assert held == []


# --- search_command ----------------------------------------------------------
def test_search_command_sonarr_series_level():
    cmd = autoblock.search_command("Sonarr", _item(series_id=42))
    assert cmd == {"name": "SeriesSearch", "seriesId": 42}


def test_search_command_radarr_movie_level():
    cmd = autoblock.search_command("Radarr", _item(movie_id=7))
    assert cmd == {"name": "MoviesSearch", "movieIds": [7]}


def test_search_command_missing_id_returns_none():
    assert autoblock.search_command("Sonarr", _item()) is None


# --- item_reason / format_action ---------------------------------------------
def test_item_reason_prefers_status_messages():
    it = _item(
        status="warning", messages=["Found executable file with extension: '.exe'"]
    )
    assert "executable" in autoblock.item_reason(it)


def test_item_reason_falls_back_to_status():
    assert autoblock.item_reason(_item(status="error")) == "error"


def test_format_action_dry_run_says_would():
    s = autoblock.format_action(True, "Sonarr", "Bad.Release", "importBlocked", 3, 3)
    assert s.startswith("WOULD blocklist [Sonarr]")
    assert "(3/3)" in s


def test_format_action_live_says_blocklisted():
    s = autoblock.format_action(False, "Radarr", "Bad.Movie", "error", 3, 3)
    assert s.startswith("Blocklisted + re-searched [Radarr]")


def test_sanitize_defuses_discord_mentions_and_backticks():
    assert "@" not in autoblock.sanitize("@everyone `rm`")
