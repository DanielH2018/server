import valheim_stats as stats

# Real shape of a line as it reaches Loki: the image wraps every console line in its own
# supervisord prefix. Terraria's image does not, which is why its parser can anchor at ^
# and this one cannot. Every parse test runs through here so a regression to anchoring
# fails loudly rather than passing on synthetic bare lines.
PREFIX = "Aug 13 16:53:42 supervisord: valheim-server "


def prefixed(msg):
    return PREFIX + msg


def test_parse_spawn():
    line = prefixed("Got character ZDOID from Testvazz : 954855457:113")
    assert stats.parse_line(line) == ("spawn", "Testvazz")


def test_parse_death_is_the_zero_zero_sentinel():
    line = prefixed("Got character ZDOID from Testvazz : 0:0")
    assert stats.parse_line(line) == ("death", "Testvazz")


def test_parse_connect():
    line = prefixed("Got handshake from client 76561198108936133")
    assert stats.parse_line(line) == ("connect", "76561198108936133")


def test_parse_disconnect():
    line = prefixed("Closing socket 76561198108936133")
    assert stats.parse_line(line) == ("disconnect", "76561198108936133")


def test_parse_restart_on_world_load():
    assert stats.parse_line(prefixed("Load world: Dedicated (Dedicated)")) == (
        "restart",
        None,
    )


def test_parse_heartbeat():
    # Verbatim from this server's log, double space included.
    line = prefixed("08/13/2026 16:03:42:  Connections 0 ZDOS:157103  sent:0 recv:0")
    assert stats.parse_line(line) == ("heartbeat", 0)


def test_parse_name_with_spaces():
    line = prefixed("Got character ZDOID from Big Boss : 954855457:113")
    assert stats.parse_line(line) == ("spawn", "Big Boss")


def test_parse_works_without_the_supervisord_prefix():
    """Bare lines must still parse — the prefix is the image's, not the protocol's."""
    assert stats.parse_line("Got character ZDOID from Solo : 0:0") == ("death", "Solo")


def test_parse_noise_returns_none():
    for msg in (
        "Failed to place all TarPit2, placed 18 out of 100",
        "Unloading 2 unused Assets to reduce memory usage.",
        "Registering lobby",
        "Opened Steam server",
        "",
    ):
        assert stats.parse_line(prefixed(msg)) is None, msg


def test_negative_zdo_id_is_a_spawn_not_a_death():
    """Only the exact 0:0 pair is death; a negative id is a normal character."""
    line = prefixed("Got character ZDOID from Testvazz : -954855457:113")
    assert stats.parse_line(line) == ("spawn", "Testvazz")


def test_unmatched_detector_flags_a_reworded_zdoid_line():
    assert stats.is_unparsed_player_line("Got character ZDOID from Bob") is True


def test_unmatched_detector_ignores_lines_that_parsed():
    line = prefixed("Got character ZDOID from Testvazz : 0:0")
    assert stats.is_unparsed_player_line(line) is False


def test_unmatched_detector_ignores_ordinary_noise():
    assert stats.is_unparsed_player_line(prefixed("Registering lobby")) is False


def _connected(st, steam_id, name, ts):
    st.apply("connect", steam_id, ts)
    st.apply("spawn", name, ts)


def test_session_playtime_accrues_between_spawn_and_disconnect():
    st = stats.StatsState()
    _connected(st, "7656119", "Bob", 1000.0)
    st.apply("disconnect", "7656119", 1060.0)
    assert st.players["Bob"]["total_playtime"] == 60.0
    assert st.players["Bob"]["sessions"] == 1


def test_respawn_after_death_does_not_start_a_second_session():
    """The spawn line fires again on respawn — the trap this guards."""
    st = stats.StatsState()
    _connected(st, "7656119", "Bob", 1000.0)
    st.apply("death", "Bob", 1030.0)
    st.apply("spawn", "Bob", 1040.0)  # respawn
    st.apply("disconnect", "7656119", 1100.0)
    assert st.players["Bob"]["sessions"] == 1
    assert st.players["Bob"]["deaths"] == 1
    assert st.players["Bob"]["total_playtime"] == 100.0


def test_death_does_not_interrupt_playtime():
    st = stats.StatsState()
    _connected(st, "7656119", "Bob", 0.0)
    st.apply("death", "Bob", 10.0)
    assert st.players["Bob"]["open_start"] == 0.0
    assert st.online_count() == 1


def test_disconnect_resolves_the_name_via_the_steamid_map():
    st = stats.StatsState()
    _connected(st, "76561198108936133", "Bob", 100.0)
    st.apply("disconnect", "76561198108936133", 200.0)
    assert st.players["Bob"]["open_start"] is None
    assert st.online_count() == 0


def test_disconnect_for_an_unknown_steamid_is_ignored():
    """A disconnect whose handshake predates our cursor must not corrupt anyone."""
    st = stats.StatsState()
    _connected(st, "111", "Bob", 100.0)
    st.apply("disconnect", "999", 200.0)
    assert st.online_count() == 1


def test_restart_closes_every_open_session():
    st = stats.StatsState()
    _connected(st, "111", "Bob", 0.0)
    _connected(st, "222", "Ann", 0.0)
    st.apply("restart", None, 50.0)
    assert st.online_count() == 0
    assert st.players["Bob"]["total_playtime"] == 50.0
    assert st.players["Ann"]["total_playtime"] == 50.0


def test_two_players_bind_to_their_own_steamids():
    st = stats.StatsState()
    _connected(st, "111", "Bob", 0.0)
    _connected(st, "222", "Ann", 10.0)
    st.apply("disconnect", "111", 100.0)
    assert st.players["Bob"]["open_start"] is None
    assert st.players["Ann"]["open_start"] == 10.0


def test_deaths_are_counted_for_a_player_never_seen_spawning():
    """Deaths key off the name directly, so they survive a missed handshake."""
    st = stats.StatsState()
    st.apply("death", "Ghost", 5.0)
    assert st.players["Ghost"]["deaths"] == 1


def test_heartbeat_records_the_servers_own_connection_count():
    st = stats.StatsState()
    st.apply("heartbeat", 3, 10.0)
    assert st.connections == 3


def test_playtime_includes_the_open_session():
    st = stats.StatsState()
    _connected(st, "111", "Bob", 100.0)
    assert st.playtime("Bob", 160.0) == 60.0


def test_render_metrics_emits_deaths_and_online():
    st = stats.StatsState()
    _connected(st, "111", "Bob", 0.0)
    st.apply("death", "Bob", 10.0)
    st.apply("heartbeat", 1, 10.0)
    out = stats.render_metrics(st, 100.0)
    assert 'valheim_player_deaths_total{player="Bob"} 1' in out
    assert "valheim_deaths_total 1" in out
    assert "valheim_players_online 1" in out
    assert "valheim_connections 1" in out
    assert 'valheim_player_playtime_seconds_total{player="Bob"} 100' in out


def test_render_metrics_escapes_quotes_in_player_names():
    st = stats.StatsState()
    st.apply("death", 'He said "hi"', 1.0)
    out = stats.render_metrics(st, 2.0)
    assert 'player="He said \\"hi\\""' in out


def test_apply_entries_counts_unmatched_and_skips_heartbeat_events():
    st = stats.StatsState()
    entries = [
        (1_000_000_000, prefixed("Got character ZDOID from Bob : 1:1")),
        (2_000_000_000, prefixed("Connections 1 ZDOS:5  sent:0 recv:0")),
        (3_000_000_000, "Got character ZDOID from Bob"),  # reworded -> unmatched
    ]
    events, max_ts = stats.apply_entries(st, entries)
    assert max_ts == 3_000_000_000
    assert st.unmatched == 1
    assert [e[2] for e in events] == ["spawn"]


def test_extract_entries_sorts_ascending():
    payload = {
        "data": {
            "result": [
                {"values": [["20", "b"], ["10", "a"]]},
            ]
        }
    }
    assert stats.extract_entries(payload) == [(10, "a"), (20, "b")]


def test_store_round_trips_deaths_and_the_steamid_map(tmp_path):
    db = str(tmp_path / "s.db")
    store = stats.Store(db)
    st = stats.StatsState()
    _connected(st, "76561198108936133", "Bob", 100.0)
    st.apply("death", "Bob", 110.0)
    store.save(st, 12345)

    reloaded = stats.Store(db).load_state()
    assert reloaded.players["Bob"]["deaths"] == 1
    assert reloaded.players["Bob"]["open_start"] == 100.0
    assert reloaded.steam_to_name["76561198108936133"] == "Bob"
    assert stats.Store(db).get_cursor() == 12345


def test_a_disconnect_after_a_restart_of_this_service_still_resolves(tmp_path):
    """The steam->name map is persisted precisely so a mid-session restart is safe."""
    db = str(tmp_path / "s.db")
    store = stats.Store(db)
    st = stats.StatsState()
    _connected(st, "111", "Bob", 100.0)
    store.save(st, 1)

    revived = stats.Store(db).load_state()
    revived.apply("disconnect", "111", 200.0)
    assert revived.players["Bob"]["sessions"] == 1
    assert revived.players["Bob"]["total_playtime"] == 100.0


def test_initial_cursor_bounds_a_fresh_db_to_the_backfill_window():
    now = 1_000_000.0
    got = stats.initial_cursor(0, False, now, 28)
    assert got == int((now - 28 * 86400) * 1e9)


def test_initial_cursor_resumes_from_a_stored_cursor():
    assert stats.initial_cursor(999, False, 1_000_000.0, 28) == 999
