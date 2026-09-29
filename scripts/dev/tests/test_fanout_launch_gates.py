"""The launch gates: what `launch` refuses before it spends an ssh connection (#2889).

Run: uv run pytest scripts/dev/tests/test_fanout_launch_gates.py
"""

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

# Roomy on both planes and holding a registered key, so a refusal here comes from a gate
# rather than from placement or the signing gate.
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
CLAIMED = [
    Issue(1, "one", "body one", ("claude",)),
    Issue(2, "two", "body two", ("claude",)),
]


def _launch(tools, tmp_path, *args):
    return main(
        [
            "launch",
            *args,
            "--orchestrator-branch",
            "o",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )


def test_live_batches_from_an_earlier_run_count_against_the_hosts_cap(tmp_path, capsys):
    """#2889: headroom alone underprices agents launched minutes ago, so count them."""
    earlier = [
        Batch(f"{n}", "daniel-box", "/w", "b", "u", [n], "t") for n in (91, 92, 93)
    ]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1,2") == 3
    err = capsys.readouterr().err
    assert "daniel-box already holds 3 live batch(es)" in err
    assert "91 in run 20260101T000010Z" in err
    assert "run `clean <run-id>`" in err
    # The headroom read happened; no launch call did.
    assert len(run.calls) == 1


def test_a_cleaned_batch_does_not_count_against_the_cap(tmp_path):
    """The red half's twin: `clean` is what frees a slot, so a removed batch must not hold one."""
    earlier = [
        Batch(f"{n}", "daniel-box", "/w", "b", "u", [n], "t", removed_at="t")
        for n in (91, 92, 93)
    ]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1,2") == 0
    assert len(run.calls) == 3  # headroom read, health read, one launch
