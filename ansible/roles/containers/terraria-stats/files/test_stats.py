import stats


def test_parse_join():
    assert stats.parse_line("DBoy has joined.") == ("join", "DBoy")


def test_parse_leave():
    assert stats.parse_line("DBoy has left.") == ("leave", "DBoy")


def test_parse_restart_listening():
    assert stats.parse_line("Listening on port 7777") == ("restart", None)


def test_parse_restart_server_started():
    assert stats.parse_line("Server started") == ("restart", None)


def test_parse_name_with_spaces():
    assert stats.parse_line("Big Boss has joined.") == ("join", "Big Boss")


def test_parse_noise_returns_none():
    for line in (
        "172.21.0.15:59682 is connecting...",
        "Saving world data: 75%",
        "Validating world save: 18%",
        "Backing up world file",
        "",
    ):
        assert stats.parse_line(line) is None


def test_unparsed_player_line_detects_drift():
    # A future console wording that no longer matches the strict patterns.
    assert stats.is_unparsed_player_line("DBoy has joined the game") is True
    assert stats.is_unparsed_player_line("DBoy has left the world") is True


def test_unparsed_player_line_false_for_valid_and_noise():
    assert stats.is_unparsed_player_line("DBoy has joined.") is False
    assert stats.is_unparsed_player_line("Saving world data: 75%") is False
    assert stats.is_unparsed_player_line("1.2.3.4:5 is connecting...") is False


def test_state_join_then_leave_accrues_playtime():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    p = st.players["DBoy"]
    assert p["total_playtime"] == 60.0
    assert p["sessions"] == 1
    assert p["open_start"] is None
    assert p["first_seen"] == 1000.0
    assert p["last_seen"] == 1060.0


def test_state_first_seen_is_sticky():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    st.apply("join", "DBoy", 2000.0)
    st.apply("leave", "DBoy", 2030.0)
    p = st.players["DBoy"]
    assert p["first_seen"] == 1000.0
    assert p["total_playtime"] == 90.0
    assert p["sessions"] == 2


def test_state_leave_without_join_is_noop():
    st = stats.StatsState()
    st.apply("leave", "Ghost", 1000.0)
    assert "Ghost" not in st.players or st.players["Ghost"]["sessions"] == 0


def test_restart_closes_open_sessions():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("join", "Pal", 1010.0)
    st.apply("restart", None, 1100.0)
    assert st.players["DBoy"]["total_playtime"] == 100.0
    assert st.players["Pal"]["total_playtime"] == 90.0
    assert st.online_count() == 0


def test_rejoin_without_leave_closes_prior_session():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("join", "DBoy", 1050.0)   # crash/rejoin, no leave
    assert st.players["DBoy"]["sessions"] == 1
    assert st.players["DBoy"]["total_playtime"] == 50.0
    assert st.players["DBoy"]["open_start"] == 1050.0
    assert st.online_count() == 1


def test_live_playtime_includes_open_session():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    assert st.playtime("DBoy", now=1040.0) == 40.0   # 0 closed + 40 live
    st.apply("leave", "DBoy", 1100.0)
    assert st.playtime("DBoy", now=9999.0) == 100.0  # closed, no live delta


def test_escape_label_value():
    assert stats.escape_label_value('a"b\\c') == 'a\\"b\\\\c'


def test_render_metrics_contains_expected_series():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    st.apply("join", "Pal", 2000.0)
    st.unmatched = 2
    out = stats.render_metrics(st, now=2030.0)
    assert 'terraria_player_playtime_seconds_total{player="DBoy"} 60' in out
    assert 'terraria_player_playtime_seconds_total{player="Pal"} 30' in out
    assert 'terraria_player_sessions_total{player="DBoy"} 1' in out
    assert "terraria_players_online 1" in out
    assert "terraria_stats_unmatched_player_lines_total 2" in out
    assert "# TYPE terraria_players_online gauge" in out


def _loki_response(entries):
    # entries: list of (ts_ns_int, line). Mimics Loki query_range JSON.
    return {"data": {"result": [
        {"stream": {"container": "terraria"},
         "values": [[str(ts), line] for ts, line in entries]}
    ]}}


def test_extract_entries_sorts_ascending():
    resp = _loki_response([(300, "c"), (100, "a"), (200, "b")])
    assert stats.extract_entries(resp) == [(100, "a"), (200, "b"), (300, "c")]


def test_extract_entries_empty():
    assert stats.extract_entries({"data": {"result": []}}) == []


def test_apply_entries_folds_and_counts_unmatched():
    st = stats.StatsState()
    entries = [
        (1_000_000_000, "DBoy has joined."),
        (5_000_000_000, "DBoy has left."),          # +4s
        (6_000_000_000, "DBoy has joined the game"),  # drift -> unmatched
        (7_000_000_000, "Saving world data: 5%"),     # noise -> ignored
    ]
    evs, maxts = stats.apply_entries(st, entries)
    assert st.players["DBoy"]["total_playtime"] == 4.0
    assert st.players["DBoy"]["sessions"] == 1
    assert st.unmatched == 1
    assert maxts == 7_000_000_000
    assert evs == [
        (1_000_000_000, "DBoy", "join", "DBoy has joined."),
        (5_000_000_000, "DBoy", "leave", "DBoy has left."),
    ]


def test_store_roundtrip_and_cursor(tmp_path):
    db = str(tmp_path / "stats.db")
    store = stats.Store(db)
    assert store.get_cursor() == 0

    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1100.0)
    store.append_events([(1_100_000_000_000, "DBoy", "leave", "DBoy has left.")])
    store.save(st, cursor_ns=1_100_000_000_000)

    # Reopen: state + cursor survive (durable source of truth).
    store2 = stats.Store(db)
    assert store2.get_cursor() == 1_100_000_000_000
    loaded = store2.load_state()
    assert loaded.players["DBoy"]["total_playtime"] == 100.0
    assert loaded.players["DBoy"]["sessions"] == 1


def test_store_preserves_open_session(tmp_path):
    db = str(tmp_path / "stats.db")
    store = stats.Store(db)
    st = stats.StatsState()
    st.apply("join", "DBoy", 2000.0)     # still online
    store.save(st, cursor_ns=2_000_000_000_000)
    loaded = stats.Store(db).load_state()
    assert loaded.players["DBoy"]["open_start"] == 2000.0
    assert loaded.online_count() == 1
