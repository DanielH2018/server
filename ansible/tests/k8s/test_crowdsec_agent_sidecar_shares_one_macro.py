"""traefik and authelia render their CrowdSec agent from one macro file, driven by one map.

`ansible/templates/crowdsec-agent.yml.j2` carries the three seeding init containers, the
`crowdsec-agent` sidecar and the volumes they share. Until #3741 each pod held a hand-kept copy
of about 120 lines, and the copies had drifted: the scratch volume carried a different name in
each pod.

`crowdsec_k8s_sidecar_agents` in group_vars maps each host app to the LAPI machine its agent
logs in as. Four readers derive from it, in roles that cannot see each other's defaults: the
sidecar's AGENT_USERNAME, the crowdsec role's machine registration loop, observability's
`crowdsec-<app>-agent` scrape jobs and netpol-baseline's :6060 grant, which
`test_crowdsec_sidecars_are_scraped.py` covers. A machine one reader names and another does not is an agent
that cannot log in, or one nothing scrapes.

The caller guard is textual, because a call and a written-out copy render the same manifest,
so only the source tells them apart. The map's readers are checked on the render, and the
registration loop by evaluating its own `loop:` expression.

Run: uv run pytest ansible/tests/k8s/test_crowdsec_agent_sidecar_shares_one_macro.py
"""

import re

from _helpers import K8S_ROLES, load_tasks, render_expr, task_named
from _k8s_render import host_context, rendered_docs
from lib import yaml_fast

# The roles whose pod carries the sidecar. Named rather than discovered: a census that globbed
# for callers would go empty on a rename and pass on nothing.
CALLERS = ("authelia", "traefik")

_IMPORT = "{% from 'crowdsec-agent.yml.j2' import"
_CALLS = (
    "crowdsec_agent_init_containers",
    "crowdsec_agent_sidecar",
    "crowdsec_agent_volumes",
)
# A container or volume the macros own, written out as a list item in the caller.
_COPIED = re.compile(
    r"^\s*- name: (crowdsec-(?:agent|hub-install|config-install|data-install|etc|data|"
    r"agent-tmp|secret))\s*$",
    re.MULTILINE,
)


def call_offences(text: str) -> list[str]:
    """One line per way this template is not just calling the shared macros."""
    offences = []
    if _IMPORT not in text:
        offences.append("does not import from crowdsec-agent.yml.j2")
    for name in _CALLS:
        if not re.search(r"\{\{\s*" + name + r"\(", text):
            offences.append(f"no {name}() call")
    for m in _COPIED.finditer(text):
        offences.append(f"`{m.group(1)}` written out instead of the macro")
    return offences


def _agents() -> dict[str, str]:
    return host_context()["crowdsec_k8s_sidecar_agents"]


def test_a_written_out_copy_is_flagged():
    """Reject case: the pre-#3741 shape, a sidecar and a volume written out beside no call."""
    copy = (
        "      containers:\n"
        "        - name: crowdsec-agent\n"
        "          image: {{ crowdsec_k8s_image }}\n"
        "      volumes:\n"
        "        - name: crowdsec-tmp\n"
        "          emptyDir: {}\n"
        "        - name: crowdsec-etc\n"
    )
    offences = call_offences(copy)
    assert "does not import from crowdsec-agent.yml.j2" in offences
    assert "no crowdsec_agent_sidecar() call" in offences
    assert "`crowdsec-agent` written out instead of the macro" in offences
    assert "`crowdsec-etc` written out instead of the macro" in offences


def test_a_bare_call_is_clean():
    text = (
        "{% from 'crowdsec-agent.yml.j2' import crowdsec_agent_init_containers, "
        "crowdsec_agent_sidecar, crowdsec_agent_volumes with context %}\n"
        "{{ crowdsec_agent_init_containers('x', log_volume='l', log_dir='/d', "
        "log_file='f', uid=1, gid=1) }}\n"
        "{{ crowdsec_agent_sidecar('x') }}\n"
        "        - name: crowdsec-key\n"
        "{{ crowdsec_agent_volumes('x') }}\n"
    )
    assert call_offences(text) == []


def test_each_caller_calls_the_macros_and_copies_nothing():
    for role in CALLERS:
        text = (K8S_ROLES / role / "templates" / "deployment.yaml.j2").read_text()
        assert call_offences(text) == [], f"{role}: {call_offences(text)}"


def test_the_agent_map_names_exactly_the_callers():
    assert set(_agents()) == set(CALLERS)


def test_each_sidecar_logs_in_as_its_mapped_machine():
    usernames = {}
    for role, _template, doc in rendered_docs():
        if role not in CALLERS or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] == "crowdsec-agent":
                env = {e["name"]: e.get("value") for e in container["env"]}
                usernames[role] = env["AGENT_USERNAME"]
    assert usernames == _agents()


def test_the_lapi_registration_loop_registers_every_mapped_machine():
    task = task_named(
        load_tasks(K8S_ROLES / "crowdsec" / "tasks" / "main.yml"),
        "Register the remote agent machines on the LAPI",
    )
    context = host_context()
    registered = render_expr(
        task["loop"],
        crowdsec_k8s_sidecar_agents=context["crowdsec_k8s_sidecar_agents"],
        crowdsec_k8s_node_agent_machines=["node-machine"],
    )
    assert sorted(registered) == sorted([*_agents().values(), "node-machine"])


def test_each_mapped_app_has_a_scrape_job():
    jobs = None
    for role, template, doc in rendered_docs():
        if (
            role == "observability"
            and "prometheus" in str(template)
            and doc.get("kind") == "ConfigMap"
        ):
            jobs = yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    assert jobs, "no prometheus ConfigMap rendered"
    names = {j["job_name"] for j in jobs if j["job_name"].endswith("-agent")}
    assert names == {f"crowdsec-{app}-agent" for app in _agents()}
