"""Tests for `probe.py applied` comparing the content of files the tree holds no render for (#6913).

Two kinds of row read `ok-meta` without a content comparison: a `/etc/cron.d` file that
`ansible.builtin.cron` writes, and a template whose render at HEAD holds a `STUB`. Each test
runs `run_applied` with a fake host read. The read answers `stat` with the want's own declared
metadata and the SHA-256 of the live text the test gives, and it answers a `rootq` hash the
probe asks about with `match`, the way `rootq matches PATH HEX` does.

The cron module's output was read off the real module (ansible-core, 2026-11-02): a
`#Ansible: <name>` line, then `<minute> <hour> <day> <month> <weekday> <user> <job>`, with `*`
for an omitted field and a trailing newline.

The contract the fix meets for a STUB render: the apply records each such file's SHA-256 in
`RECORD_DIR/<role>`, one `<hex>  <path>` line per file, as `sha256sum` prints it. The probe reads
that record through the host read's `files` and compares the live hash with it.
"""

import hashlib
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import applied, applied_tasks
from lib.exit_codes import PROBE_FAILED, PROBE_OK

RECORD_DIR = "/var/lib/homelab/setup-applied-sha256.d"

REGISTRY_GC_CRON = (
    "#Ansible: Registry garbage collection\n"
    "20 4 * * 0 root /usr/local/bin/registry-gc.sh 2>&1 | logger -t registry-gc\n"
)
CRON_TASK = """\
- name: Schedule it
  ansible.builtin.cron: {name: Nightly x, minute: "5", hour: "3", user: root, job: /usr/local/bin/x.sh, cron_file: x}
"""
CRON_TEXT = "#Ansible: Nightly x\n5 3 * * * root /usr/local/bin/x.sh\n"
STUB_TASK = """\
- name: Install the token file
  ansible.builtin.template: {src: token.j2, dest: /etc/x/token, mode: "0600", owner: root, group: root}
"""
STUB_CTX = {"x_api_token": "STUB"}
APPLIED_TOKEN = "token=s3cr3t\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _record(role: str, texts: dict[str, str]) -> dict[str, str]:
    """The apply-time record of `texts`, keyed by the record's path."""
    lines = "".join(f"{_sha(text)}  {path}\n" for path, text in texts.items())
    return {f"{RECORD_DIR}/{role}": lines}


def _reader(wants, live: dict[str, str], files: dict[str, str]):
    """A host read answering each want as declared, with `live` texts and `files` on disk."""
    by_path = {w.path: w for w in wants}

    def stat_row(path: str, rootq: dict) -> dict:
        want = by_path.get(path)
        if (want is None and path not in files) or (want and not want.present):
            return {"status": "absent"}
        row = {"status": "ok", "mode": "0644", "owner": "root", "group": "root"}
        if want is not None:
            row.update(
                {k: v for k in ("mode", "owner", "group") if (v := getattr(want, k))}
            )
        text = live.get(path, files.get(path))
        live_hex = _sha(text) if text is not None else want and want.sha256
        if live_hex is not None:
            row["sha256"] = live_hex
            if rootq.get(path):
                row["match"] = rootq[path] == live_hex
        return row

    def read(plan: dict, host: str) -> dict:
        rootq = plan.get("rootq") or {}
        return {
            "host": host,
            "user": "ubuntu",
            "stat": {p: stat_row(p, rootq) for p in plan.get("stat", [])},
            "files": {
                p: {"status": "ok", "text": files[p]}
                if p in files
                else {"status": "absent"}
                for p in plan.get("files", [])
            },
        }

    return read


def _rows(out: str) -> dict[str, tuple[str, str]]:
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("/"):
            rows[parts[1]] = (parts[0], line)
    return rows


def _run_tmp(tmp_path, capsys, tasks, ctx, live, files):
    role = tmp_path / "x"
    for sub, name, text in (
        ("tasks", "main.yml", tasks),
        ("templates", "token.j2", "token={{ x_api_token }}\n"),
    ):
        (role / sub).mkdir(parents=True, exist_ok=True)
        (role / sub / name).write_text(text)
    wants, _ = applied_tasks.expected(role, "daniel-box", ctx)
    ns = SimpleNamespace(role="x", host="daniel-box", dry_run=False)
    code = applied.run_applied(
        ns,
        read=_reader(wants, live, files),
        setup=tmp_path,
        render_ctx=lambda role_dir, host: ctx,
    )
    return code, _rows(capsys.readouterr().out)


def test_the_registry_gc_cron_want_carries_the_hash_of_the_line_the_module_writes():
    role = applied.SETUP / "k8s_host_files"
    wants, _ = applied_tasks.expected(
        role, "daniel-box", applied.context(role, "daniel-box")
    )
    cron = next(w for w in wants if w.path == "/etc/cron.d/registry-gc")
    assert cron.sha256 == _sha(REGISTRY_GC_CRON), cron


def test_a_cron_file_as_the_module_writes_it_reads_ok(tmp_path, capsys):
    """The task omits `day`, `month` and `weekday`, which the module writes as `*`."""
    live = {"/etc/cron.d/x": CRON_TEXT}
    code, rows = _run_tmp(tmp_path, capsys, CRON_TASK, {}, live, {})
    assert rows["/etc/cron.d/x"][0] == "ok", rows
    assert code == PROBE_OK


def test_a_cron_file_edited_by_hand_is_drift(tmp_path, capsys):
    live = {"/etc/cron.d/x": CRON_TEXT.replace("5 3 ", "0 9 ")}
    code, rows = _run_tmp(tmp_path, capsys, CRON_TASK, {}, live, {})
    assert rows["/etc/cron.d/x"][0] == "DRIFT", rows
    assert code == PROBE_FAILED


def test_a_stub_render_whose_live_hash_matches_the_apply_record_reads_ok(
    tmp_path, capsys
):
    live = {"/etc/x/token": APPLIED_TOKEN}
    record = _record("x", {"/etc/x/token": APPLIED_TOKEN})
    code, rows = _run_tmp(tmp_path, capsys, STUB_TASK, STUB_CTX, live, record)
    assert rows["/etc/x/token"][0] == "ok", rows
    assert code == PROBE_OK


def test_a_stub_render_edited_after_the_apply_is_drift(tmp_path, capsys):
    live = {"/etc/x/token": "token=edited\n"}
    record = _record("x", {"/etc/x/token": APPLIED_TOKEN})
    code, rows = _run_tmp(tmp_path, capsys, STUB_TASK, STUB_CTX, live, record)
    assert rows["/etc/x/token"][0] == "DRIFT", rows
    assert code == PROBE_FAILED


@pytest.mark.parametrize(
    "path", ["/etc/cron.d/registry-gc", "/usr/local/bin/registry-gc.sh"]
)
def test_k8s_host_files_compares_the_registry_gc_rows_rather_than_reading_ok_meta(
    capsys, path
):
    """The issue's Verify-by: both rows read ok when the host holds what the apply wrote."""
    script = "#!/bin/bash\nPUSH_TOKEN=applied\n"
    live = {"/etc/cron.d/registry-gc": REGISTRY_GC_CRON, "/usr/local/bin/registry-gc.sh": script}  # fmt: skip
    record = _record("k8s_host_files", {"/usr/local/bin/registry-gc.sh": script})
    role = applied.SETUP / "k8s_host_files"
    wants, _ = applied_tasks.expected(
        role, "daniel-box", applied.context(role, "daniel-box")
    )
    ns = SimpleNamespace(role="k8s_host_files", host="daniel-box", dry_run=False)
    applied.run_applied(ns, read=_reader(wants, live, record))
    status, line = _rows(capsys.readouterr().out)[path]
    assert status == "ok", line
