"""`scripts/validate/alert_rules.py`'s Python phase, with the tools faked.

The pytest shards install no promtool, amtool, pint or lokitool, so every tool call here goes
through a fake runner. What the tools themselves reject is proven by CI's `alert_rules.py
--prove-red` step, which runs the real binaries against the fixtures under
`fixtures/alert_rules/`.

Run: uv run pytest scripts/validate/tests/test_validate_alert_rules.py
"""

import re

import yaml

from lib import yaml_fast
from lib.repo_paths import REPO
from validate import alert_conventions, alert_rules


def _proof_runner(bad_code: int, bad_output: str, good_code: int = 0):
    """A runner that answers each proof's good fixture with `good_code` and its bad one with
    `bad_code` and `bad_output`."""

    def run(argv: list[str]) -> tuple[int, str]:
        if any(argv[-1].endswith(p.bad) for p in alert_rules.PROOFS):
            return bad_code, bad_output
        return good_code, ""

    return run


def test_the_render_produces_every_floor_file_is_clean():
    found = alert_rules.rendered_files()
    # Non-vacuity: the floor names concrete files, so an emptied FLOOR fails here.
    assert {"prometheus.yml", "alertmanager.yml", "homelab.tmpl"} <= alert_rules.FLOOR
    assert "scrape_configs:" in found["prometheus.yml"]
    assert "receivers:" in found["alertmanager.yml"]
    assert '{{ define "homelab.title" }}' in found["homelab.tmpl"]
    assert alert_rules.floor_problems(found) == []


def test_a_render_missing_a_floor_file_is_flagged():
    problems = alert_rules.floor_problems({"prometheus.yml": "scrape_configs: []\n"})
    assert any("alertmanager.yml" in p for p in problems), problems
    assert not any("prometheus.yml" in p for p in problems), problems


def test_every_discovered_input_gets_its_tool_check(tmp_path):
    rules = tmp_path / "rules"
    tests = tmp_path / "tests"
    loki_rules = tmp_path / "loki-rules"
    rules.mkdir()
    tests.mkdir()
    loki_rules.mkdir()
    (rules / "meta.yaml").write_text("groups: []\n")
    (tests / "meta.test.yaml").write_text("tests: []\n")
    (loki_rules / "logs.yaml").write_text("groups: []\n")
    config = tmp_path / "prometheus.yml"
    am_config = tmp_path / "alertmanager.yml"

    checks = alert_rules.tool_checks(
        {"prometheus.yml": config, "alertmanager.yml": am_config},
        rules,
        tests,
        loki_rules,
    )

    assert checks == [
        ["amtool", "check-config", str(am_config)],
        ["promtool", "check", "config", "--syntax-only", str(config)],
        ["promtool", "check", "rules", str(rules / "meta.yaml")],
        ["promtool", "test", "rules", str(tests / "meta.test.yaml")],
        ["pint", *alert_rules.PINT_ARGS, str(rules)],
        ["lokitool", "rules", "lint", "--dry-run", str(loki_rules / "logs.yaml")],
    ]


def test_the_tree_with_tools_that_pass_is_clean():
    assert alert_rules.check_tree(run=lambda argv: (0, "")) == []


def test_a_tool_rejecting_the_rendered_config_is_flagged():
    problems = alert_rules.check_tree(run=lambda argv: (1, "FAILED: bad field"))
    config = [p for p in problems if "check config --syntax-only" in p]
    assert len(config) == 1, problems
    assert "FAILED: bad field" in config[0]


def test_a_lokitool_rejection_of_a_loki_rule_file_is_flagged():
    problems = alert_rules.check_tree(
        run=lambda argv: (
            (1, "could not parse expression") if argv[0] == "lokitool" else (0, "")
        )
    )
    assert len(problems) == 1, problems
    assert "lokitool rules lint --dry-run" in problems[0], problems
    assert (
        problems[0].split("`")[1].endswith("loki-homelab/files/rules/homelab-logs.yaml")
    )


def test_a_rejected_alertmanager_config_is_flagged():
    problems = alert_rules.check_tree(
        run=lambda argv: (1, "undefined receiver") if argv[0] == "amtool" else (0, "")
    )
    assert len(problems) == 1, problems
    assert "amtool check-config" in problems[0] and "undefined receiver" in problems[0]


def _alertmanager_render() -> tuple[str, str, str]:
    """The real Secret template's text, its rendered `alertmanager.yml` and `homelab.tmpl`."""
    found = alert_rules.rendered_files()
    return (
        alert_rules.ALERTMANAGER_SOURCE.read_text(),
        found["alertmanager.yml"],
        found["homelab.tmpl"],
    )


def test_go_templates_that_survive_the_render_are_clean():
    source, rendered, templates = _alertmanager_render()
    # Non-vacuity: named members of both inputs, so a renamed template or an emptied raw
    # block fails here rather than leaving the check with nothing to require.
    assert '{{ template "homelab.title" . }}' in alert_rules.RAW_BLOCK.findall(source)
    assert set(alert_rules.TEMPLATE_DEFINE.findall(templates)) == {
        "homelab.title",
        "homelab.message",
    }
    assert alert_rules.go_template_problems(source, rendered, templates) == []


def test_a_parseable_go_expression_the_render_consumed_is_flagged():
    """The case amtool cannot see. The message reference is rewritten outside its raw block
    in a form Jinja parses, so the source no longer holds it and the render drops it. The
    `define` in homelab.tmpl still requires it."""
    source, rendered, templates = _alertmanager_render()
    reference = "'{% raw %}{{ template \"homelab.message\" . }}{% endraw %}'"
    assert source.count(reference) == 2, "the fixture edit no longer matches the source"
    edited = source.replace(reference, "'{{ template_message | default(\"\") }}'")
    consumed = rendered.replace('{{ template "homelab.message" . }}', "")
    problems = alert_rules.go_template_problems(edited, consumed, templates)
    assert len(problems) == 1 and 'template "homelab.message"' in problems[0], problems


def test_a_raw_block_the_render_lost_is_flagged():
    source, rendered, templates = _alertmanager_render()
    consumed = rendered.replace(alert_rules.WATCHDOG_STATUS, "")
    problems = alert_rules.go_template_problems(source, consumed, templates)
    assert any(alert_rules.WATCHDOG_STATUS in p for p in problems), problems


def test_a_template_with_its_raw_blocks_removed_does_not_render(tmp_path):
    """Jinja refuses the Go expressions outright, through the validator's own environment."""
    from validate import k8s_manifests as validator

    stripped = tmp_path / alert_rules.ALERTMANAGER_SOURCE.name
    stripped.write_text(
        alert_rules.ALERTMANAGER_SOURCE.read_text()
        .replace("{% raw %}", "")
        .replace("{% endraw %}", "")
    )
    ctx = validator.render_context(
        alert_rules.ROLE_DIR,
        overrides={"container_item": validator.k8s_entries()[alert_rules.ROLE]},
    )
    env = validator.template_env(tmp_path)
    env.globals["lookup"] = validator.make_lookup(ctx)

    rendered, err = validator.render_or_error(env, stripped.name, ctx)

    assert rendered is None and err, rendered


def test_a_render_that_fails_is_flagged_rather_than_raised():
    def fail() -> dict[str, str]:
        raise RuntimeError("observability/alertmanager-secret.yaml.j2 failed to render")

    problems = alert_rules.check_tree(run=lambda argv: (0, ""), render=fail)
    assert problems == ["observability/alertmanager-secret.yaml.j2 failed to render"]


def test_a_homelab_tmpl_that_defines_nothing_is_flagged():
    source, rendered, _ = _alertmanager_render()
    problems = alert_rules.go_template_problems(source, rendered, "")
    assert problems and "defines no template" in problems[0], problems


def test_tools_that_accept_good_and_reject_bad_fixtures_are_clean():
    markers = "\n".join(p.marker for p in alert_rules.PROOFS)
    assert alert_rules.prove_red(run=_proof_runner(1, markers)) == []


def test_a_tool_that_accepts_its_bad_fixture_is_flagged():
    problems = alert_rules.prove_red(run=_proof_runner(0, ""))
    assert len(problems) == len(alert_rules.PROOFS), problems
    assert all("which it must reject" in p for p in problems), problems


def test_a_tool_that_never_ran_is_flagged():
    """A missing binary also exits non-zero. Read as a rejection, it would pass the proof."""
    problems = alert_rules.prove_red(
        run=_proof_runner(127, "No such file or directory", good_code=127)
    )
    assert len(problems) == 2 * len(alert_rules.PROOFS), problems
    assert any("which it must accept" in p for p in problems), problems
    assert any("without saying" in p for p in problems), problems


def test_every_proof_names_fixtures_that_exist():
    """Non-vacuity: one proof per tool command, each with both fixtures committed."""
    commands = {(p.tool, *p.args) for p in alert_rules.PROOFS}
    assert commands == {
        ("promtool", "check", "rules"),
        ("promtool", "test", "rules"),
        ("amtool", "check-config"),
        ("promtool", "check", "config", "--syntax-only"),
        ("pint", *alert_rules.PINT_ARGS),
        ("lokitool", "rules", "lint", "--dry-run"),
    }
    # promtool test rules has two proofs, and the set above holds the command once. The
    # second shows condition 3 of the third `service` source rests on promtool.
    assert [p.bad for p in alert_rules.PROOFS if p.args == ("test", "rules")] == [
        "bad-rules.test.yaml",
        "bad-job-service-rules.test.yaml",
    ]
    for proof in alert_rules.PROOFS:
        assert (alert_rules.FIXTURES / proof.good).is_file(), proof.good
        assert (alert_rules.FIXTURES / proof.bad).is_file(), proof.bad


def test_ci_provisions_the_tools_then_runs_the_red_proof():
    """The red proof exists only as this CI step; dropping it leaves the tools unproven."""
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    provision = workflow.find("run: scripts/validate/alert_tools.sh\n")
    prove = workflow.find(
        "run: uv run python scripts/validate/alert_rules.py --prove-red\n"
    )
    hooks = workflow.find("- name: Run hooks")
    assert -1 < provision < prove < hooks, (provision, prove, hooks)
    assert re.search(
        r'entry = "uv run --frozen python scripts/validate/alert_rules\.py"',
        (REPO / "prek.toml").read_text(),
    )


def test_the_tree_rules_meet_every_convention_is_clean():
    groups, keys = alert_rules.rendered_rule_keys()
    # Non-vacuity: the floor and the list name the group, and the file holds its five alerts.
    assert "alerting-meta" in alert_conventions.GROUP_FLOOR
    assert groups == [
        "alerting-meta",
        "workload-service",
        "k8s-workloads",
        "k8s-services",
        "loki-homelab",
        "flux",
        "ingress-probes",
        "host-checks",
        "initial-setup-checks",
        "gitops-checks",
        "pi-checks",
        "other-setup-checks",
        "deploy-pipeline",
        "gate-jobs",
        "registry",
        "bridge-exporter",
        "container-checks",
        "kuma-cutover",
        "scrape-targets",
        "log-pipeline",
        "edge",
        "storage",
        "hosts",
        "extra-probes",
        "pod-producers",
        "pods",
    ]
    assert keys == {
        "alerting-meta.yaml",
        "workload-service.yaml",
        "k8s-workloads.yaml",
        "k8s-services.yaml",
        "loki-homelab.yaml",
        "flux.yaml",
        "ingress-probes.yaml",
        "host-checks.yaml",
        "initial-setup-checks.yaml",
        "gitops-checks.yaml",
        "pi-checks.yaml",
        "other-setup-checks.yaml",
        "deploy-pipeline.yaml",
        "gate-jobs.yaml",
        "registry.yaml",
        "bridge-exporter.yaml",
        "container-checks.yaml",
        "kuma-cutover.yaml",
        "scrape-targets.yaml",
        "log-pipeline.yaml",
        "edge.yaml",
        "storage.yaml",
        "hosts.yaml",
        "extra-probes.yaml",
        "pod-producers.yaml",
        "pods.yaml",
    }
    doc = yaml_fast.safe_load(
        (alert_rules.RULES_DIR / "alerting-meta.yaml").read_text()
    )
    assert {rule["alert"] for rule in doc["groups"][0]["rules"]} == {
        "Watchdog",
        "PrometheusRuleReloadFailed",
        "AlertmanagerNotificationsFailing",
        "AlertmanagerMetricsMissing",
        "PrometheusRuleEvaluationFailures",
    }
    # The ingress-probes rules take `service` from their scrape job, so the conventions read
    # the rendered prometheus.yml, as check_tree hands it to them.
    prometheus_yml = alert_rules.rendered_files()["prometheus.yml"]
    assert alert_rules.rule_problems(groups, keys, prometheus_yml=prometheus_yml) == []


def test_the_loki_rule_files_their_list_and_their_configmap_agree_is_clean():
    groups, keys = alert_rules.rendered_loki_rule_keys()
    # Non-vacuity: the floor, the list and the ConfigMap all name the one group.
    assert alert_rules.LOKI_GROUP_FLOOR == {"homelab-logs"}
    assert groups == ["homelab-logs"]
    assert keys == {"homelab-logs.yaml"}
    assert alert_rules.loki_group_problems(groups, keys) == []


def test_a_loki_rule_file_the_list_does_not_name_is_flagged(tmp_path):
    for stem in ("homelab-logs", "stray"):
        (tmp_path / f"{stem}.yaml").write_text("groups: []\n")

    problems = alert_rules.check_tree(
        run=lambda argv: (0, ""),
        render_loki_rules=lambda: (["homelab-logs"], {"homelab-logs.yaml"}),
        loki_rules_dir=tmp_path,
    )

    assert problems == [
        "loki-homelab/files/rules/stray.yaml: loki_homelab_rule_groups does not name it, "
        "so it never reaches the loki-homelab-rules ConfigMap. Add the group to the list."
    ]


def test_a_placeholder_in_the_rendered_alertmanager_config_is_clean():
    """C1 renders `email` as `${email}` once R2 lands; only the sources are refused."""
    found = alert_rules.rendered_files()
    found["alertmanager.yml"] = found["alertmanager.yml"].replace(
        "smtp_from:", "smtp_from: ${email} #", 1
    )
    assert "${email}" in found["alertmanager.yml"]
    problems = alert_rules.check_tree(run=lambda argv: (0, ""), render=lambda: found)
    assert problems == []


def test_the_live_pint_config_names_the_pinned_prometheus_address():
    """live.hcl reaches Prometheus by its pinned ClusterIP, so the two must stay one value."""
    defaults = yaml_fast.safe_load(
        (REPO / "ansible/roles/k8s/observability/defaults/main.yml").read_text()
    )
    live = (alert_rules.PINT_OFFLINE.parent / "live.hcl").read_text()
    uris = re.findall(r'^\s*uri\s*=\s*"([^"]+)"', live, re.M)
    assert uris == [f"http://{defaults['observability_prometheus_cluster_ip']}:9090"], (
        uris
    )
    assert re.search(r"^\s*required\s*=\s*true\s*$", live, re.M), (
        "live.hcl must set required = true, or an unreachable Prometheus is only a warning"
    )


def _tree_with_a_job_service_alert(tmp_path, target_labels):
    """`check_tree` over the real alerting-meta rules plus an alert that takes `service` from
    job `x`, with the real render's `prometheus.yml` gaining that job.

    Args:
        target_labels: The labels job `x`'s one target sets.
    """
    rules_dir = tmp_path / "rules"
    tests_dir = tmp_path / "tests"
    rules_dir.mkdir()
    tests_dir.mkdir()
    rules = yaml_fast.safe_load(
        (alert_rules.RULES_DIR / "alerting-meta.yaml").read_text()
    )
    tests = yaml_fast.safe_load(
        (alert_rules.RULE_TESTS_DIR / "alerting-meta.test.yaml").read_text()
    )
    rules["groups"][0]["rules"].append(
        {
            "alert": "FixtureProbeFailed",
            "expr": 'probe_success{job="x"} == 0',
            "labels": {"severity": "warning"},
        }
    )
    tests["tests"][0]["alert_rule_test"].append(
        {
            "eval_time": "1m",
            "alertname": "FixtureProbeFailed",
            "exp_alerts": [{"exp_labels": {"severity": "warning", "service": "a"}}],
        }
    )
    (rules_dir / "alerting-meta.yaml").write_text(yaml.safe_dump(rules))
    (tests_dir / "alerting-meta.test.yaml").write_text(yaml.safe_dump(tests))
    found = alert_rules.rendered_files()
    config = yaml_fast.safe_load(found["prometheus.yml"])
    config["scrape_configs"].append(
        {
            "job_name": "x",
            "static_configs": [
                {"targets": ["https://a.example"], "labels": target_labels}
            ],
        }
    )
    found["prometheus.yml"] = yaml.safe_dump(config)
    return alert_rules.check_tree(
        run=lambda argv: (0, ""),
        render=lambda: found,
        render_rules=lambda: (["alerting-meta"], {"alerting-meta.yaml"}),
        rules_dir=rules_dir,
        tests_dir=tests_dir,
    )


def test_an_alert_whose_rendered_scrape_job_sets_service_is_clean(tmp_path):
    """The rendered `prometheus.yml` reaches the third `service` source's condition 2."""
    assert _tree_with_a_job_service_alert(tmp_path, {"service": "a"}) == []


def test_an_alert_whose_rendered_scrape_job_sets_no_service_is_flagged(tmp_path):
    problems = _tree_with_a_job_service_alert(tmp_path, {"origin": "daniel-box"})
    assert len(problems) == 1, problems
    assert (
        "selects job 'x', which has a target whose labels set no service" in problems[0]
    )
