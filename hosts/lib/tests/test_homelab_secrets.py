"""`hosts/lib/homelab-secrets`: the decrypt, the credentials and the outputs, against a stub `sops`.

Each case is one of H4's *Verification* cases for steps 1 to 4 and 6 of the script, and the
cases that guard a rule come as pairs: the run the design asks for, and a fixture that breaks the
rule. The script runs in this process with `HOMELAB_ROOT` pointing at a temporary tree, and
`_chown` is recorded instead of run, because the tests are not root. The restart queue (step 5)
has its own cases in test_homelab_secrets_queue.py; both suites build their tree from
_secrets_host.py.

Run: uv run pytest hosts/lib/tests/test_homelab_secrets.py
"""

import ast
import os
import re
import sys

import pytest
from _secrets_host import SCRIPT, Host


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


def test_a_run_writes_credentials_and_outputs_with_one_decrypt(host):
    host.decrypts_to(push_token="tok", nut_monitor_password="pw")
    host.credentials = ["push_token"]
    host.output(
        "etc/nut/upsmon.conf",
        "pw=${nut_monitor_password}\n",
        persist=True,
        keys=["nut_monitor_password"],
    )
    host.output("app/env", "T=${push_token}\n", keys=["push_token"], mode="0600")

    assert host.run() == 0

    assert host.decrypts() == 1
    assert host.read("/run/credstore/push_token") == "tok"
    assert host.mode("/run/credstore") == 0o700
    assert host.mode("/run/credstore/push_token") == 0o600
    assert host.read("/var/lib/homelab-secrets/etc/nut/upsmon.conf") == "pw=pw\n"
    assert host.read("/run/homelab-secrets/app/env") == "T=tok\n"
    assert host.mode("/var/lib/homelab-secrets/etc/nut/upsmon.conf") == 0o640
    assert host.mode("/run/homelab-secrets/app/env") == 0o600
    for directory in (
        "/var/lib/homelab-secrets",
        "/var/lib/homelab-secrets/etc",
        "/var/lib/homelab-secrets/etc/nut",
    ):
        assert host.mode(directory) == 0o755
        assert (str(host.path(directory)), "root", "root") in host.chowns
    upsmon_chowns = [c[1:] for c in host.chowns if "/etc/nut/.upsmon.conf." in c[0]]
    assert upsmon_chowns == [("root", "nut")]


def test_a_missing_key_fails_that_output_writes_the_others_and_exits_non_zero(host):
    host.decrypts_to(present="here")
    host.credentials = ["present", "absent"]
    host.output("a/good", "${present}", keys=["present"])
    host.output("a/bad", "${absent}", keys=["absent"])

    assert host.run() == 1

    assert host.read("/run/homelab-secrets/a/good") == "here"
    assert host.read("/run/credstore/present") == "here"
    assert not host.path("/run/homelab-secrets/a/bad").exists()
    assert not host.path("/run/credstore/absent").exists()


def test_a_failed_decrypt_writes_nothing_and_deletes_nothing(host):
    host.output("etc/nut/upsmon.conf", "${k}", persist=True, keys=["k"])
    host.credentials = ["k"]
    kept = {
        "/var/lib/homelab-secrets/etc/nut/upsmon.conf": "old",
        "/run/credstore/k": "old",
        "/run/credstore/stale": "old",
        "/run/homelab-secrets/unlisted": "old",
    }
    for absolute, text in kept.items():
        host.path(absolute).parent.mkdir(parents=True, exist_ok=True)
        host.path(absolute).write_text(text)

    assert host.run() == 1  # no plain.json: the stub sops exits 1

    assert {a: host.read(a) for a in kept} == kept
    assert host.chowns == []


def test_a_successful_decrypt_changes_the_same_tree(host):
    # The accept half of the pair above: with the age key present the same tree is rewritten.
    host.decrypts_to(k="new")
    host.output("etc/nut/upsmon.conf", "${k}", persist=True, keys=["k"])
    host.credentials = ["k"]
    host.path("/run/credstore").mkdir(parents=True)
    host.path("/run/credstore/stale").write_text("old")

    assert host.run() == 0

    assert host.read("/var/lib/homelab-secrets/etc/nut/upsmon.conf") == "new"
    assert not host.path("/run/credstore/stale").exists()


def escape(parts):
    """C1's rule: a literal `$` followed by `$`, `{` or a secret reference renders as `$$`."""
    rendered = ""
    for i, (kind, text) in enumerate(parts):
        if kind == "ref":
            rendered += "${" + text + "}"
            continue
        escaped = re.sub(r"\$(?=[${])", "$$", text)
        before_ref = i + 1 < len(parts) and parts[i + 1][0] == "ref"
        rendered += (
            escaped[:-1] + "$$" if before_ref and text.endswith("$") else escaped
        )
    return rendered


LITERAL = "a $$ b ${VAR} c $x $"
PARTS = [("lit", LITERAL), ("ref", "tok"), ("lit", " end $")]


def two_pass(template, allowed, values):
    """The fixture the design flags: it replaces `$${` first and `${<key>}` second."""
    template = template.replace("$${", "\0{").replace("$$", "$")
    for key, value in values.items():
        template = template.replace("${" + key + "}", value)
    return template.replace("\0{", "${")


def test_the_round_trip_returns_literal_text_byte_for_byte(host):
    expected = LITERAL + "SECRET" + " end $"

    assert host.module.substitute(escape(PARTS), {"tok"}, {"tok": "SECRET"}) == expected


def test_a_two_pass_substitution_is_flagged_by_the_round_trip(host):
    assert (
        two_pass(escape(PARTS), {"tok"}, {"tok": "SECRET"})
        != LITERAL + "SECRET" + " end $"
    )


def test_the_value_of_a_key_is_not_substituted_again(host):
    assert (
        host.module.substitute("${a}${b}", {"a", "b"}, {"a": "${b}", "b": "$$"})
        == "${b}$$"
    )


@pytest.mark.parametrize(
    "template",
    ["${unclosed", "${a:-default}", "${}", "x ${ y", "${not_listed}"],
    ids=["no-close", "default-form", "empty", "mid-line", "unlisted-key"],
)
def test_an_unclosed_or_unlisted_placeholder_fails_that_output(host, template):
    host.decrypts_to(a="1", not_listed="2")
    host.output("bad", template, keys=["a"])
    host.output("good", "${a}", keys=["a"])

    assert host.run() == 1

    assert not host.path("/run/homelab-secrets/bad").exists()
    assert host.read("/run/homelab-secrets/good") == "1"


def test_a_closed_listed_placeholder_renders(host):
    host.decrypts_to(a="1")
    host.output("fine", "${a} $${a} $", keys=["a"])

    assert host.run() == 0
    assert host.read("/run/homelab-secrets/fine") == "1 ${a} $"


def test_an_output_no_longer_listed_is_deleted_under_both_roots(host):
    host.decrypts_to(k="v")
    host.output("keep/me", "${k}", keys=["k"])
    old = [
        "/run/homelab-secrets/gone/old",
        "/var/lib/homelab-secrets/etc/nut/old",
        "/run/homelab-secrets/keep/.me.x.tmp",
    ]
    state = [
        "/var/lib/homelab-secrets/.digests",
        "/var/lib/homelab-secrets/.hmac-key",
        "/run/homelab-secrets/pending.json",
    ]
    for absolute in old + state:
        host.path(absolute).parent.mkdir(parents=True, exist_ok=True)
        host.path(absolute).write_text("x" if absolute in old else "[]")

    assert host.run() == 0

    assert [a for a in old if host.path(a).exists()] == []
    assert not host.path("/run/homelab-secrets/gone").exists()
    assert not host.path("/var/lib/homelab-secrets/etc").exists()
    assert [a for a in state if not host.path(a).exists()] == []
    assert host.read("/run/homelab-secrets/keep/me") == "v"


def test_a_write_that_fails_leaves_the_old_file_and_no_temporary(host, monkeypatch):
    host.decrypts_to(k="new")
    host.output("f", "${k}", keys=["k"])
    host.path("/run/homelab-secrets").mkdir(parents=True)
    host.path("/run/homelab-secrets/f").write_text("old")
    monkeypatch.setattr(
        os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full"))
    )

    assert host.run() == 1

    assert host.read("/run/homelab-secrets/f") == "old"
    names = sorted(p.name for p in host.path("/run/homelab-secrets").iterdir())
    assert names == ["f", "pending.lock"]  # step 5's lock file, and no .tmp


@pytest.mark.parametrize(
    "destination", ["../escape", "a//b", "/abs", ".digests", "a/./b"]
)
def test_a_destination_outside_the_output_root_fails_the_whole_run(host, destination):
    host.decrypts_to(k="v")
    host.output("fine", "${k}", keys=["k"])
    host.output("ignored", "x")
    host.outputs[-1]["destination"] = destination

    assert host.run() == 1

    assert not host.path("/run/homelab-secrets/fine").exists()
    assert host.decrypts() == 0


def test_an_ordinary_nested_destination_passes_the_same_check(host):
    host.decrypts_to(k="v")
    host.output("a/b/c", "${k}", keys=["k"])

    assert host.run() == 0


def parses_under_3_12(source):
    try:
        ast.parse(source, feature_version=(3, 12))
    except SyntaxError:
        return False
    return True


def test_the_script_runs_under_the_hosts_python3_with_the_3_12_grammar():
    assert SCRIPT.read_text().startswith("#!/usr/bin/python3\n")
    assert SCRIPT.stat().st_mode & 0o111
    assert parses_under_3_12(SCRIPT.read_text())
    assert not parses_under_3_12(SCRIPT.read_text() + 'label = t"{name}"\n')


ENV_TEMPLATE = "A='${a}'\nB='${b}'\n"


@pytest.mark.parametrize(
    "bad",
    ["it's", "'", "line1\nline2", "trailing\n", "cr\rhere"],
    ids=["inner-quote", "lone-quote", "newline", "trailing-newline", "carriage-return"],
)
def test_a_dotenv_output_refuses_a_value_with_a_quote_or_line_break(host, bad, capsys):
    host.decrypts_to(a=bad, b="fine")
    host.output("compose.env", ENV_TEMPLATE, keys=["a", "b"], format="dotenv")
    host.output("plain.env", "${b}", keys=["b"])

    assert host.run() == 1

    assert not host.path("/run/homelab-secrets/compose.env").exists()
    assert host.read("/run/homelab-secrets/plain.env") == "fine"
    err = capsys.readouterr().err
    assert "the value of a holds a quote or a line break" in err
    assert bad.strip() not in err


def test_a_failure_reaches_the_journal_at_err_priority(host, tmp_path, monkeypatch):
    # Every line the script prints is a failure, so `journalctl -p err` must find each (#6202).
    journal = tmp_path / "journal"
    journal.touch()
    st = journal.stat()
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
    host.decrypts_to(a="it's", b="fine")
    host.output("compose.env", ENV_TEMPLATE, keys=["a", "b"], format="dotenv")
    with open(journal, "w") as err:
        monkeypatch.setattr(sys, "stderr", err)
        assert host.run() == 1
    lines = journal.read_text().splitlines()
    assert lines and all(line.startswith("<3>homelab-secrets: ") for line in lines), (
        lines
    )


def test_a_dotenv_output_renders_a_value_with_no_quote_or_line_break(host):
    value = 'p@ss "w0rd" $x #y \\ {z} `t`'
    host.decrypts_to(a=value, b="fine")
    host.output("compose.env", ENV_TEMPLATE, keys=["a", "b"], format="dotenv")

    assert host.run() == 0
    assert host.read("/run/homelab-secrets/compose.env") == f"A='{value}'\nB='fine'\n"


def test_an_output_with_no_format_renders_the_same_values_unchecked(host):
    host.decrypts_to(a="it's\nhere", b="fine")
    host.output("other.env", ENV_TEMPLATE, keys=["a", "b"])

    assert host.run() == 0
    assert host.read("/run/homelab-secrets/other.env") == "A='it's\nhere'\nB='fine'\n"


def test_the_dotenv_check_covers_only_the_keys_the_output_lists(host):
    host.decrypts_to(a="fine", b="fine", unrelated="it's")
    host.output("compose.env", ENV_TEMPLATE, keys=["a", "b"], format="dotenv")

    assert host.run() == 0


def test_a_refused_dotenv_value_leaves_the_old_file_in_place(host):
    host.decrypts_to(a="old", b="fine")
    host.output("compose.env", ENV_TEMPLATE, keys=["a", "b"], format="dotenv")
    assert host.run() == 0

    host.decrypts_to(a="new'value", b="fine")

    assert host.run() == 1
    assert host.read("/run/homelab-secrets/compose.env") == "A='old'\nB='fine'\n"


def test_an_unknown_format_makes_the_manifest_invalid(host):
    host.decrypts_to(a="fine", b="fine")
    host.output("compose.env", ENV_TEMPLATE, keys=["a", "b"], format="yaml")

    assert host.run() == 1
    assert host.decrypts() == 0
