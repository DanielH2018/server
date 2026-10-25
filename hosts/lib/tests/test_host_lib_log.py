"""host_lib's shared log helpers: `logfmt`, `log`, the crash hook, `deploy_event`, `write_textfile`.

#6161 moved the deployer's journald-aware `log` here so every host-shipped script prints one
way, and gave the hand-written logfmt emitters one quoting rule. #6164 put the deploy
annotation's line here, so a failure and a success are written by the same function.
"""

import os
import stat
import sys

import pytest

import host_lib


def _journal(monkeypatch, path):
    """Make `path` the stream systemd's `JOURNAL_STREAM` names, and return it open."""
    path.touch()
    st = path.stat()
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
    return open(path, "w")


def test_logfmt_quotes_only_a_value_a_logfmt_parser_would_split():
    fields = {"a": "x", "b": "", "c": "two words", "d": 'q"t', "e": None, "f": "k=v"}
    assert host_lib.logfmt(fields) == 'a=x b= c="two words" d="q\\"t" e= f="k=v"'


def test_logfmt_always_quotes_a_named_key_even_when_empty():
    assert host_lib.logfmt({"lock": 0, "holder": ""}, always_quote=("holder",)) == (
        'lock=0 holder=""'
    )


def test_log_prefixes_the_tag_and_appends_fields(capsys, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    host_lib.log("built", host_lib.WARNING, tag="homelab-builder", sha="abc", n=2)
    assert capsys.readouterr().out == "homelab-builder: built sha=abc n=2\n"


def test_a_leveled_line_carries_its_priority_on_every_line_on_the_journal(
    tmp_path, monkeypatch
):
    with _journal(monkeypatch, tmp_path / "journal") as out:
        host_lib.log("failed:\nTraceback", host_lib.ERR, tag="x", stream=out)
        host_lib.log("plain", tag="x", stream=out)
    assert (
        tmp_path / "journal"
    ).read_text() == "<3>x: failed:\n<3>Traceback\nx: plain\n"


def test_the_excepthook_files_an_uncaught_traceback_at_err_on_the_journal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    with _journal(monkeypatch, tmp_path / "journal") as err:
        monkeypatch.setattr(sys, "stderr", err)
        host_lib.install_excepthook()
        try:
            raise RuntimeError("tick crashed")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    lines = (tmp_path / "journal").read_text().splitlines()
    assert lines[0] == "<3>Traceback (most recent call last):"
    assert lines[-1] == "<3>RuntimeError: tick crashed"
    assert all(line.startswith("<3>") for line in lines)


def test_the_excepthook_leaves_the_traceback_plain_off_the_journal(capsys, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    host_lib.install_excepthook()
    try:
        raise RuntimeError("tick crashed")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    err = capsys.readouterr().err
    assert err.startswith("Traceback (most recent call last):\n")
    assert err.endswith("RuntimeError: tick crashed\n") and "<3>" not in err


def test_a_successful_deploy_event_is_the_line_every_emitter_wrote_before():
    assert host_lib.deploy_event("a,b", "abcd1234", source="gitops") == (
        "event=deploy services=a,b sha=abcd1234 result=ok source=gitops"
    )
    assert host_lib.deploy_event("full", "abcd1234") == (
        "event=deploy services=full sha=abcd1234 result=ok"
    )


def test_a_failed_deploy_event_carries_its_result_and_whole_seconds():
    assert host_lib.deploy_event(
        "sonarr", "abcd1234", "rollback", duration_s=61.9, source="gitops"
    ) == (
        "event=deploy services=sonarr sha=abcd1234 result=rollback duration_s=61 "
        "source=gitops"
    )


def test_write_textfile_writes_0644_and_leaves_only_the_target(tmp_path):
    path = tmp_path / "check.prom"
    host_lib.write_textfile(str(path), "a 1\n")
    host_lib.write_textfile(str(path), b"a 2\n")
    assert path.read_text() == "a 2\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert os.listdir(tmp_path) == ["check.prom"]


def test_a_failed_textfile_write_removes_its_temp_file(tmp_path):
    # A directory at the target makes the final `os.replace` fail after the temp file exists.
    (tmp_path / "check.prom").mkdir()
    with pytest.raises(IsADirectoryError):
        host_lib.write_textfile(str(tmp_path / "check.prom"), "a 1\n")
    assert os.listdir(tmp_path) == ["check.prom"]
