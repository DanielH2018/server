"""The Alloy pods pipeline files each line under one `level`, whatever format the pod logs in.

The level stages are run out of the rendered config by `_alloy_pods_pipeline.pod_level`, so
each case asserts on what ships. Alloy compiles the line filters as RE2; Python's `re` stands
in, which is equivalent for the bracket classes, bounded repeats and alternation they use.
The lines are real shapes from the cluster's pods, trimmed, except the loguru one.
"""

import pytest

from _alloy_pods_pipeline import pod_level, stage_keys

ESC = "\x1b"

TAGGED = [
    ("level=INFO msg=started", "info"),
    (
        'level=ERROR msg="History query failed" error="failed to query Prometheus"',
        "error",
    ),
    (
        'time="2026-11-19T00:45:55Z" level=warning msg="Proxy error: read failed"',
        "warn",
    ),
    (
        '{"level":"info","ts":"2026-11-19T00:52:27Z","msg":"created temporary db file"}',
        "info",
    ),
    (
        '{"level":"error","time":"2026-11-19T00:46:17Z","message":"router has no domains"}',
        "error",
    ),
    ('{"level": "error", "message": "json.dumps spaces its separators"}', "error"),
    ("level = warn msg=spaced", "warn"),
    ("2026-11-19 00:52:41.547 | DEBUG    | app.poll:run:42 - polling", "debug"),
    (
        f"{ESC}[2m2026-11-19T00:52:42Z{ESC}[0m {ESC}[32m INFO{ESC}[0m HTTP request",
        "info",
    ),
    (f"time=00:52:42 {ESC}[33mwarn{ESC}[39m slow query", "warn"),
    (
        'E1119 00:52:35.626951       1 reflector.go:227] "Failed to watch" err="not found"',
        "error",
    ),
    (
        "[18:48:42] [WRN] [9] WebSocketConnection: WS error receiving data: the remote party closed",
        "warn",
    ),
    (
        '2026-11-18T18:52:16.380-0600\tINFO\tSending HTTP request\t{"url": "http://localhost:8686/"}',
        "info",
    ),
    (
        f"{ESC}[32m2026-11-18 18:52:00.365-06:00 INFO (MainThread) [homeassistant.components.automation]{ESC}[0m",
        "info",
    ),
    ("2026/11/19 00:46:25 [notice] 1#1: signal 29 (SIGIO) received", "info"),
    ('level=fatal msg="starting kubernetes: init cluster datastore"', "fatal"),
]


@pytest.mark.parametrize(("line", "level"), TAGGED)
def test_each_format_files_its_line_under_the_normalised_level_is_clean(line, level):
    assert pod_level(line) == level


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ('level=info msg="retrying" error="connection reset"', "info"),
        ('{"level":"info","error_level":"error","msg":"handled"}', "info"),
        ('msg="error calling scheduler" err="rpc error"', None),
        (
            '10.42.0.1 - - [19/Nov/2026:00:52:43 +0000] "GET /v2/ HTTP/1.1" 200 2 "" "kube-probe/1.37"',
            None,
        ),
    ],
)
def test_a_word_outside_the_level_token_sets_no_level_is_flagged(line, level):
    """Loki's own detected_level guesses from words, which filed a `[WRN]` line as an error."""
    assert pod_level(line) == level


def test_the_level_ships_as_structured_metadata_never_a_label_is_clean():
    """A `level` label would split every pod stream into one per level."""
    assert stage_keys("stage.structured_metadata") == {"level"}
    assert "level" not in stage_keys("stage.labels")
