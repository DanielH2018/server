"""The shard weight gate's log parsers, over generated reports.

`parse_durations` reads pytest's `--durations` report, `runner_weights` combines one report
per CI leg, and `leg_speed` scales each leg by its runner's cold compile time. The fixed logs in
`test_pytest_shard_durations.py` never carry `::` or brackets inside a node id, many phases per
file, or a noise prefix on every line. These tests generate them. They run under the profile
`tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/dev/tests/ci/test_shard_weight_gate_properties.py
"""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from dev import shard_weight_gate as gate

_FILES = st.sampled_from(
    ["tests/test_a.py", "scripts/x/tests/test_b.py", "k8s/roles/r/tests/test_c.py"]
)
_PHASES = st.sampled_from(["setup", "call", "teardown"])
_SECONDS = st.decimals(0, 500, places=2)
_TEST_IDS = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs", "Zl", "Zp")),
    max_size=30,
) | st.sampled_from(["test_x[a::b]", "TestK::test_y[1-2]", "[", "]::[", "::"])
_ROWS = st.lists(st.tuples(_FILES, _PHASES, _SECONDS, _TEST_IDS), max_size=25)
_ANSI = st.sampled_from(["", "\x1b[32m", "\x1b[1;31m", "\x1b[0m"])
_STAMPS = st.sampled_from(["", "2026-11-27T01:02:03.4567890Z "])


@st.composite
def _reports(draw):
    rows = draw(_ROWS)
    lines = []
    for file, phase, seconds, test_id in rows:
        line = f"{seconds}s {phase} {file}::{test_id}"
        lines.append(draw(_STAMPS) + draw(_ANSI) + line + draw(_ANSI))
    expected: dict[str, float] = {}
    for file, _, seconds, _ in rows:
        expected[file] = expected.get(file, 0.0) + float(seconds)
    return "\n".join(lines), {f: round(v, 3) for f, v in expected.items()}


def _compile_line(files: int, milliseconds: int) -> str:
    return f"Bytecode compiled {files} files in {milliseconds}ms"


@given(_reports())
def test_parse_durations_sums_each_files_rows_through_log_noise(report):
    text, expected = report
    parsed = gate.parse_durations(gate._LOG_NOISE.sub("", text))
    assert parsed == pytest.approx(expected, abs=1e-3)


@given(
    st.lists(_reports(), min_size=1, max_size=4),
    st.lists(st.integers(1, 5000), min_size=4, max_size=4),
    st.randoms(use_true_random=False),
)
def test_runner_weights_ignores_report_order(reports, files, rng):
    texts = [
        f"{_compile_line(n, n)}\n{text}"
        for (text, _), n in zip(reports, files, strict=False)
    ]
    shuffled = texts[:]
    rng.shuffle(shuffled)
    assert gate.runner_weights(shuffled) == gate.runner_weights(texts)


@given(
    st.integers(1, 5000),
    st.integers(1, 50),
    st.integers(2, 10),
    _SECONDS,
)
def test_leg_speed_scales_with_cold_compile_time(files, per_file_ms, k, seconds):
    base = _compile_line(files, files * per_file_ms)
    scaled = _compile_line(files, files * per_file_ms * k)
    assert gate.leg_speed(scaled) == pytest.approx(k * gate.leg_speed(base))

    row = f"{seconds}s call tests/test_a.py::test_x"
    contribution = gate.runner_weights([f"{base}\n{row}"])["tests/test_a.py"]
    shrunk = gate.runner_weights([f"{scaled}\n{row}"])["tests/test_a.py"]
    assert shrunk == pytest.approx(contribution / k, abs=0.01)


@example("..s call tests/test_a.py::test_x")
@example("Bytecode compiled 5 files in ..ms")
@given(st.text())
def test_parsers_never_raise_on_arbitrary_text(text):
    gate.parse_durations(text)
    gate.leg_speed(text)
