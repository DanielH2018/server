#!/usr/bin/env python3
"""Tests for the compose-template validator's $$-escaping check.

Docker Compose interpolates `$VAR` / `${VAR}` / `$(...)` in string values at parse
time, so a shell `$` meant for the container must be written `$$` in the template.
`recreate: auto` and `ansible-lint` both miss this; a lone `$` either gets blanked
(missing env) or interpolated, silently breaking a healthcheck/command. The check
is context-aware: it only inspects command/entrypoint/healthcheck.test, so the
intentional `${GID-...}` interpolation some services use in `environment:` is not
flagged.

Run: uv run pytest scripts/test_validate_compose_templates.py
"""
import importlib.util
import os

_MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validate_compose_templates.py")
_spec = importlib.util.spec_from_file_location("validate_compose_templates", _MOD)
vct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vct)


def _docs(spec):
    """One rendered compose doc with a single service named 'svc'."""
    return [{"services": {"svc": spec}}]


# --- clean: $ correctly doubled, or no relevant key --------------------------

def test_doubled_dollar_in_healthcheck_is_clean():
    docs = _docs({"healthcheck": {"test": ["CMD-SHELL", 'x=$$(date) && [ "$${x:-0}" ]']}})
    assert vct.find_dollar_escape_bugs(docs) == []


def test_doubled_dollar_in_command_list_is_clean():
    # prometheus-style node-exporter arg with an escaped regex anchor
    docs = _docs({"command": ["--collector.filesystem.mount-points-exclude=^/(sys|proc)($$|/)"]})
    assert vct.find_dollar_escape_bugs(docs) == []


def test_no_command_or_healthcheck_is_clean():
    assert vct.find_dollar_escape_bugs(_docs({"image": "nginx"})) == []


def test_environment_interpolation_is_not_flagged():
    # the deliberate Compose ${GID-...} interpolation (crowdsec/traefik) lives in
    # environment:, which the check intentionally does NOT inspect.
    docs = _docs({"environment": {"GID": "${GID-1000}"}, "command": "run --port 8080"})
    assert vct.find_dollar_escape_bugs(docs) == []


# --- buggy: a lone (un-doubled) $ in a shell context -------------------------

def test_lone_dollar_in_healthcheck_is_flagged():
    docs = _docs({"healthcheck": {"test": ["CMD-SHELL", "curl http://$HOSTNAME/ || exit 1"]}})
    bugs = vct.find_dollar_escape_bugs(docs)
    assert len(bugs) == 1
    svc, key, snippet = bugs[0]
    assert svc == "svc" and key == "healthcheck.test" and "$HOSTNAME" in snippet


def test_lone_dollar_in_command_string_is_flagged():
    docs = _docs({"command": "sh -c 'echo $(hostname)'"})
    bugs = vct.find_dollar_escape_bugs(docs)
    assert len(bugs) == 1 and bugs[0][1] == "command"


def test_lone_dollar_in_entrypoint_list_is_flagged():
    docs = _docs({"entrypoint": ["sh", "-c", "exec $APP"]})
    bugs = vct.find_dollar_escape_bugs(docs)
    assert len(bugs) == 1 and bugs[0][1] == "entrypoint"


def test_triple_dollar_still_flags_the_interpolated_remainder():
    # $$$ = one escaped $$ plus a lone ${x} -> still a (likely) bug.
    docs = _docs({"command": "echo $$${x}"})
    assert len(vct.find_dollar_escape_bugs(docs)) == 1


# --- structure walking / robustness -----------------------------------------

def test_walks_all_services_and_attributes_the_right_one():
    docs = [{"services": {"a": {"command": "ok"}, "b": {"command": "echo $X"}}}]
    bugs = vct.find_dollar_escape_bugs(docs)
    assert [s for s, _, _ in bugs] == ["b"]


def test_tolerates_non_service_docs():
    assert vct.find_dollar_escape_bugs([None, {"version": "3"}, "junk", {"services": "x"}]) == []
