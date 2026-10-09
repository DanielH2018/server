"""`probe.py kuma-drift`: what is missing, not what is down.

`monitors` answers "what is down". `kuma-drift` answers "what is missing", which `monitors`
structurally cannot — it counts the exporter's own set, so a monitor that is gone rather than
down leaves the ratio at N/N up.
"""

from pathlib import Path

from check_table import CHECKS
from diagnostics.probe_lib import kuma_live, monitors

TEMPLATE_SAMPLE = """\
stringData:
  discord.json: |
    {"type": "notification", "name": "Homelab Alerts", "active": true}
  root-disk.json: |
    {"type": "push", "name": "Root Disk", "interval": 60, "push_token": "x"}
  peer-backup.json: |
    {"type": "push", "name": "WG Pi Peer Backup", "interval": 216000, "push_token": "x"}
  grafana.json: |
    {"type": "http", "name": "k3s Grafana", "url": "https://g.example", "interval": 60}
{% if etcd_snapshot_push_token | default('') %}
  etcd.json: |
    {"type": "push", "name": "Off-box etcd Snapshot", "interval": 90000, "push_token": "x"}
{% endif %}
"""


def drift(declared, live, kuma_age_seconds, **kwargs):
    """`format_kuma_drift` against a Kuma that holds every declared monitor."""
    return monitors.format_kuma_drift(
        declared, live, kuma_age_seconds, created=set(declared), **kwargs
    )


def test_parse_declared_monitors_reads_names_types_and_gating():
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    # Notifications are not monitors and never appear in monitor_status — counting them would
    # make every run report two phantom missing entries.
    assert "Homelab Alerts" not in declared
    assert declared["Root Disk"] == {
        "type": "push",
        "interval": 60,
        "gated": False,
        "gate": None,
    }
    assert declared["k3s Grafana"]["type"] == "http"
    assert declared["Off-box etcd Snapshot"]["gated"] is True
    # The condition is captured, not just the fact of being gated — that is what lets the
    # caller resolve the secret instead of assuming it is unset.
    assert declared["Off-box etcd Snapshot"]["gate"] == (
        "etcd_snapshot_push_token | default('')",
    )


def _series(name, status="1"):
    return {"metric": {"monitor_name": name}, "value": [1720000000, status]}


def test_monitors_names_the_declared_monitors_the_exporter_has_not_exported():
    # Minutes after a Kuma pod replacement the exporter had 89 of 105 declared
    # monitors, and `monitors` read "89/89 up" — the 16 unreported tiles included the etcd
    # snapshot and secret-rotation dead-men. The ratio stays (it is still what is down), and
    # the shortfall is named beside it. Exit code stays 0: absence is kuma-drift's verdict.
    data = {"data": {"result": [_series("Root Disk"), _series("k3s Grafana")]}}
    text, code = monitors.format_monitor_status(data, declared_total=4)
    assert code == 0
    assert text.startswith("2/2 monitors up\n")
    assert "2 of 4 declared monitors have no monitor_status series" in text
    assert "kuma-drift" in text


def test_monitors_is_silent_about_coverage_when_every_declared_monitor_is_exported():
    data = {"data": {"result": [_series("Root Disk"), _series("k3s Grafana")]}}
    text, code = monitors.format_monitor_status(data, declared_total=2)
    assert (text, code) == ("2/2 monitors up", 0)


def test_declared_monitor_count_reads_the_real_template():
    # The count `run_monitors` hands to the coverage line comes from the real static-monitors
    # template, so a template that moved or a parse that returns nothing shows up here as a
    # missing count rather than as a coverage line that is silent forever.
    declared = monitors.declared_monitor_count()
    assert declared is not None and declared >= 90, declared


def test_declared_monitor_count_leaves_gated_declarations_out(tmp_path):
    # A gated monitor whose secret is unset is never live; counting it would make the coverage
    # line print on every healthy run, which is the line nobody reads when it matters.
    template = tmp_path / "static-monitors.yaml.j2"
    template.write_text(TEMPLATE_SAMPLE)
    assert monitors.declared_monitor_count(str(template)) == 3


def test_kuma_drift_reports_a_declared_monitor_that_is_not_live():
    # The tile is absent from the exporter, not down, so `monitors` would report 81/81 up.
    # Long-uptime Kuma, so PENDING cannot be the explanation.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "k3s Grafana"}
    text, code = drift(declared, live, 86400 * 3)
    assert code == 1
    assert "WG Pi Peer Backup: declared, not live" in text


def test_kuma_drift_calls_a_push_monitor_pending_inside_its_own_interval():
    # Kuma exports a monitor only after it beats, so a restart empties every push series. A
    # monitor whose interval has not elapsed since the restart is not yet due — flagging it
    # would make this check fail after every deploy.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"k3s Grafana"}
    text, code = drift(declared, live, 30)
    assert code == 0
    assert "no beat due yet" in text
    assert "declared, not live" not in text


TEMPLATED_INTERVAL_SAMPLE = """\
stringData:
  root-disk.json: |
    {"type": "push", "name": "Root Disk", "interval": 60, "push_token": "x"}
  drill.json: |
    {"type": "push", "name": "etcd Restore Drill (full)", "interval": {{ etcd_drill_full_kuma_interval_s }}, "push_token": "x"}
  ups.json: |
    {"type": "push", "name": "UPS Secondary", "interval": {{ nut_host_watchdog_interval_minutes | default(10) * 60 * 2 }}, "push_token": "x"}
  orphan.json: |
    {"type": "push", "name": "Unresolvable", "interval": {{ no_such_variable }}, "push_token": "x"}
"""
TEMPLATED_INTERVAL_VARS = {"etcd_drill_full_kuma_interval_s": 3024000}


def test_parse_declared_monitors_evaluates_a_templated_interval_against_the_variables():
    # A digits-only match read `{{ etcd_drill_full_kuma_interval_s }}` as None, so the monthly
    # drill tile could never be pending and read as drift after every Kuma restart.
    declared = monitors.parse_declared_monitors(
        TEMPLATED_INTERVAL_SAMPLE, variables=TEMPLATED_INTERVAL_VARS
    )
    assert declared["etcd Restore Drill (full)"]["interval"] == 3024000
    # An expression, not a name: evaluated the way Ansible would write it, filter first.
    assert declared["UPS Secondary"]["interval"] == 1200
    assert declared["Root Disk"]["interval"] == 60


def test_an_unresolvable_templated_interval_reads_as_none_and_files_as_missing():
    # Fail loud: a tile whose interval this check cannot read must not be excused as pending.
    declared = monitors.parse_declared_monitors(
        TEMPLATED_INTERVAL_SAMPLE, variables=TEMPLATED_INTERVAL_VARS
    )
    assert declared["Unresolvable"]["interval"] is None
    text, code = drift(declared, {"Root Disk"}, 30)
    assert code == 1
    assert "Unresolvable: declared, not live" in text


def test_kuma_drift_calls_a_templated_interval_tile_pending_inside_its_interval():
    declared = monitors.parse_declared_monitors(
        TEMPLATED_INTERVAL_SAMPLE, variables=TEMPLATED_INTERVAL_VARS
    )
    del declared["Unresolvable"]
    # 3024000s is 35 days; a Kuma pod a day old is well inside it. Past the literal tile's own
    # 60s+slack, so the literal one is the drift and the templated one is not — the pair that
    # shows the templated tile is classified by its interval, not waved through.
    text, code = drift(declared, set(), 86400)
    assert code == 1
    assert "etcd Restore Drill (full): no beat due yet (3024000s interval)" in text
    assert "UPS Secondary: declared, not live" in text
    assert "Root Disk: declared, not live" in text


def test_kuma_drift_treats_every_type_as_pending_after_a_restart():
    # The first live run of this check reported 58 monitors missing 88 seconds into a rollout.
    # Kuma's exporter emits a monitor only after it beats, and that applies to http/port/dns
    # tiles too — restricting the pending rule to push monitors made a routine deploy look like
    # mass drift. The slack covers the exporter's and Prometheus's scrape lag on top.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = drift(declared, set(), 88)
    assert code == 0
    assert "k3s Grafana: no beat due yet" in text


def test_kuma_drift_fails_loud_when_the_pod_age_is_unreadable():
    # Same rule as `health`'s unreadable restart time: an unknown age must not silently excuse
    # a missing monitor, or the check reports green exactly when it cannot tell.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = drift(declared, {"k3s Grafana"}, None)
    assert code == 1
    assert "Root Disk: declared, not live" in text


def test_kuma_drift_reports_a_live_monitor_nobody_declared():
    # `kubectl apply` leaves orphaned objects behind, and AutoKuma's on_delete=delete only
    # removes what it still tracks — a monitor whose declaration was dropped can outlive it.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana", "Retired Tile"}
    text, code = drift(declared, live, 86400)
    assert code == 1
    assert "Retired Tile: live, not declared" in text


def test_kuma_drift_skips_a_monitor_whose_gate_is_genuinely_unset():
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = drift(
        declared,
        live,
        86400,
        gate_states={"etcd_snapshot_push_token | default('')": False},
    )
    assert code == 0
    assert "Off-box etcd Snapshot" in text
    assert "gated off" in text


def test_kuma_drift_reports_drift_when_the_gate_is_set_but_the_monitor_is_absent():
    """The reason `gate` exists.

    etcd_snapshot_push_token is set and Off-box etcd Snapshot is not live — which must not be
    called correctly skipped. A gated monitor that vanishes was invisible twice: absent from
    the exporter, and excused by the drift check written to catch exactly that.
    """
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    # Past the monitor's own 90000s interval, so `pending` cannot absorb it — a gate-set
    # monitor inside its interval is still legitimately pending, not drift.
    text, code = drift(
        declared,
        live,
        86400 * 3,
        gate_states={"etcd_snapshot_push_token | default('')": True},
    )
    assert code == 1
    assert "Off-box etcd Snapshot: declared, not live" in text
    assert "gated off" not in text


#
# kuma-drift --pi: scoping the declared/live sets to daniel-pi's own monitors. Uses the REAL
# static-monitors template rather than TEMPLATE_SAMPLE — pi_monitor_names() is a census over the
# actual file, and a fixture with an invented "-pi" key would prove nothing about it.

with open(monitors.STATIC_MONITORS_PATH) as _f:
    REAL_STATIC_MONITORS_TEXT = _f.read()


def test_every_interval_in_the_real_template_resolves_through_the_real_variables():
    # The fixture tests above hand in their own variables; this is the proof the default
    # loader reaches the values the template actually reads — a group_var, a role default and
    # a nut_host default behind `| default()`. A loader that returned {} would leave every
    # templated tile at None and the fixture tests green.
    declared = monitors.parse_declared_monitors(REAL_STATIC_MONITORS_TEXT)
    assert declared["etcd Restore Drill (full)"]["interval"] == 3024000
    # uptime_kuma_k8s_bridge_push_interval
    assert declared["Root Disk"]["interval"] == 1200
    assert declared["UPS Secondary (daniel-box)"]["interval"] == 1200
    unresolved = sorted(n for n, s in declared.items() if s["interval"] is None)
    assert unresolved == [], f"intervals this check cannot read: {unresolved}"


def test_pi_monitor_names_finds_at_least_the_known_daniel_pi_monitors():
    names = monitors.pi_monitor_names(REAL_STATIC_MONITORS_TEXT)
    # A frozenset a caller can name, not just a count — see CLAUDE.md's non-vacuity rule.
    assert {"Daniel Pi Host", "Daniel Pi WG Easy", "Daniel Pi Recovery"} <= names
    assert len(names) >= 3


def test_pi_monitor_names_excludes_a_k8s_monitor_that_merely_contains_pi():
    # "Pi-hole k8s DNS" is declared under the "pihole-k8s-dns" key — "pi" is a substring of
    # "pihole", not a hyphen-delimited token, and it runs on k3s, not daniel-pi.
    names = monitors.pi_monitor_names(REAL_STATIC_MONITORS_TEXT)
    assert "Pi-hole k8s DNS" not in names


def test_is_pi_monitor_key_requires_pi_as_its_own_token():
    assert monitors.is_pi_monitor_key("daniel-pi-host")
    assert monitors.is_pi_monitor_key("monitor-bridge-pi")
    assert not monitors.is_pi_monitor_key("pihole-k8s-dns")


def test_kuma_drift_pi_reports_a_missing_pi_monitor_without_cluster_noise():
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    pi_names = {
        "Root Disk"
    }  # stand in: only "Root Disk" is "pi-plane" for this fixture
    scoped_declared = {n: s for n, s in declared.items() if n in pi_names}
    text, code = drift(scoped_declared, set(), 86400 * 3)
    assert code == 1
    assert "Root Disk: declared, not live" in text
    # Scoping means a cluster-only miss (k3s Grafana, never in pi_names) must not appear.
    assert "k3s Grafana" not in text


def test_kuma_drift_says_so_when_a_gate_cannot_be_read():
    """An unreadable gate and an unset one must not look alike — that equivalence is what let
    the case above stay silent. Unreadable does not fail the exit code (no age key on this
    host is a normal state), but it is named rather than swallowed."""
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = drift(
        declared,
        live,
        86400,
        gate_states={"etcd_snapshot_push_token | default('')": None},
    )
    assert code == 0
    assert "could not be read" in text
    assert "gated off" not in text


def test_run_kuma_drift_pi_end_to_end_reports_a_missing_pi_monitor(monkeypatch, capsys):
    """`kuma-drift --pi` through the real template and a stubbed Prometheus response.

    Live carries every DECLARED name except "Daniel Pi Recovery" and every non-Pi monitor
    besides — the fixture that proves the cluster's other ~75 monitors do not leak into a
    Pi-scoped run.
    """
    import json

    from diagnostics.probe_lib import cli_parser, core

    pi_names = monitors.pi_monitor_names(REAL_STATIC_MONITORS_TEXT)
    live = (pi_names | {"k3s Grafana", "Root Disk"}) - {"Daniel Pi Recovery"}

    def fake_fetch(url, resolve=None):
        result = [
            {"metric": {"monitor_name": name}, "value": [0, "1"]} for name in live
        ]
        return json.dumps({"data": {"result": result}})

    # Kuma holds the tile; it is only absent from the exporter.
    held = [{"name": n} for n in live | pi_names]
    page = json.dumps({"publicGroupList": [{"monitorList": held}]})
    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "get_status", lambda url, resolve: (200, page))
    monkeypatch.setattr(core, "k8s_endpoint", lambda host: ("https://k.test", None))
    monkeypatch.setattr(kuma_live, "pod_age_seconds", lambda cluster: 86400 * 3)

    ns = cli_parser._build_parser().parse_args(["kuma-drift", "--pi", "--no-secrets"])
    assert monitors.run_kuma_drift(ns) == 1
    out = capsys.readouterr().out
    assert "Daniel Pi Recovery: declared, not live" in out
    # Cluster-only monitors (declared, live, or both) must not leak into a --pi run.
    assert "k3s Grafana" not in out
    assert "Root Disk" not in out


def test_resolve_gate_states_covers_only_the_gates_whose_monitor_is_absent():
    """The narrowing is a deliberate sops-cost bound: one decrypt per gate, so it is not paid
    for a monitor that is live and needs no explanation for why it might not be.

    Shared with `postflight.check_kuma_drift` through one constructor rather than a second
    copy — a second caller is exactly how the gate_states argument comes to be omitted.

    Asserted on the returned KEYS rather than on a recorded call list, so this needs no age
    key and no patch: the narrowing IS which gates appear. `no_secrets` then pins the other
    half — a deliberate non-read maps to None ("could not be read", rendered as unverified),
    never to False, which would excuse the monitor. That conflation is the miss this guards
    against. Both gates name secrets the real store declares, so `no_secrets` is what keeps
    them unread rather than the names being unknown.
    """
    declared = {
        "Live Gated": {
            "type": "push",
            "interval": 60,
            "gated": True,
            "gate": ("docs_refresh_push_token | default('')",),
        },
        "Absent Gated": {
            "type": "push",
            "interval": 60,
            "gated": True,
            "gate": ("etcd_snapshot_push_token | default('')",),
        },
        "Ungated": {"type": "http", "interval": 60, "gated": False, "gate": None},
    }
    assert monitors.resolve_gate_states(declared, {"Live Gated"}, no_secrets=True) == {
        "etcd_snapshot_push_token | default('')": None
    }


def test_an_undeclared_gate_key_reads_as_genuinely_unset_is_clean():
    """A key that is not in the store at all is an unset gate.

    A gated monitor whose token was never minted correctly renders away, which is exactly the
    False arm. It reported None
    instead, so two §9.1 lines read "gated on <var>, which could not be read" on every run and
    trained the reader to skim the arm that catches a real failure to read a secret.
    """
    verdict = monitors.judge_gate_read(
        "never_declared_push_token", None, {"other_token"}
    )
    assert verdict is False


def test_a_declared_gate_key_that_will_not_resolve_is_flagged_unverified():
    """The rejecting half, and the arm the split exists to protect.

    A key the store DOES declare whose value still cannot be read is the broken host this arm
    was written for — no age key, the secret tool missing. Collapsing it into False is the
    regression `judge_gate_read`'s own docstring forbids, and without this case there is no
    evidence the None arm survives the split.
    """
    verdict = monitors.judge_gate_read(
        "etcd_snapshot_push_token", None, {"etcd_snapshot_push_token"}
    )
    assert verdict is None


def test_an_unreadable_key_list_stays_unverified():
    """A key list that cannot be read proves nothing about whether the key is declared.

    Ordering matters here: the plaintext key-list parse succeeds on a host with no age key, so
    consulting it FIRST would report every gate as unset. `declared_secret_names` returning
    None is the same refusal one level down, and this is `gate_var_state` passing it through.
    """
    assert monitors.judge_gate_read("etcd_snapshot_push_token", None, None) is None


def test_a_resolvable_gate_key_is_judged_by_its_value():
    """A value that was read decides on its own — the key list is not consulted.

    `declared` is passed as the empty set, which would make an undeclared key False if the
    order were wrong; the value still wins.
    """
    assert (
        monitors.judge_gate_read("etcd_snapshot_push_token", "s3cret\n", set()) is True
    )
    assert monitors.judge_gate_read("etcd_snapshot_push_token", "\n", set()) is False


def test_declared_secret_names_reads_the_real_stores_key_list():
    """Against the committed file, not a fixture — the plaintext-keys assumption is the point.

    The store encrypts values and not key names, which is what lets this read answer "is the
    key declared" with no age key and no decrypted value. If that ever stopped holding, the
    split above would report every gate as unset on this host. Asserted against named members
    rather than a count, so a shrinking store names what went missing.
    """
    names = monitors.declared_secret_names()
    assert names is not None
    assert {"etcd_snapshot_push_token", "jellyfin_api_key", "sonarr_api_key"} <= names
    # The metadata block is not a secret, and treating it as one would make a gate named
    # after it resolve.
    assert "sops" not in names
    assert "never_declared_push_token" not in names


def test_the_bridge_tile_loop_expands_to_one_declaration_per_table_row(
    tmp_path, monkeypatch
):
    table = tmp_path / "roles/k8s/b/files/t.py"
    table.parent.mkdir(parents=True)
    table.write_text(
        'ROWS = (Row(display="Alpha", fn=x.a), Row(display="Beta", fn=x.b))\n'
    )
    text = (
        "stringData:\n"
        "{% for row in lookup('file', playbook_dir ~ '/roles/k8s/b/files/t.py')"
        " | py_table('ROWS') %}\n"
        "  {{ row.kuma_id }}.json: |\n"
        '    {"type": "push", "name": {{ row.display | to_json }}, "interval": 60'
        '{% if row.x %}, "x": 1{% endif %}}\n'
        "{% endfor %}\n"
        "  after.json: |\n"
        '    {"type": "http", "name": "After", "interval": 30}\n'
    )
    declared = monitors.parse_declared_monitors(text, root=tmp_path)
    assert set(declared) == {"Alpha", "Beta", "After"}
    assert declared["Beta"] == {
        "type": "push",
        "interval": 60,
        "gated": False,
        "gate": None,
    }


def test_the_real_template_declares_every_bridge_tile_ungated():
    """#3781 moved the bridge tiles into a loop; kuma-drift must still list each one."""
    declared = monitors.parse_declared_monitors(
        Path(monitors.STATIC_MONITORS_PATH).read_text()
    )
    bridge = {row.display: declared.get(row.display) for row in CHECKS}
    assert len(bridge) >= 44 and "Traefik 421" in bridge
    assert all(d is not None and d["gated"] is False for d in bridge.values()), bridge
    assert not [name for name in declared if "{" in name]


def test_the_real_template_declares_every_ingress_tile_ungated():
    """#3690 moved the ingress tiles into a loop over containers_list; kuma-drift must list each."""
    declared = monitors.parse_declared_monitors(
        Path(monitors.STATIC_MONITORS_PATH).read_text()
    )
    for name in ("k3s Grafana", "k3s Authelia Portal", "k3s littlelink"):
        assert declared.get(name) == {
            "type": "http",
            "interval": 60,
            "gated": False,
            "gate": None,
        }, name
