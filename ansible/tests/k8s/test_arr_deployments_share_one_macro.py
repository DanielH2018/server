"""radarr and sonarr render their Deployment from one shared macro, not from two copies.

`ansible/templates/arr-deployment.yml.j2` carries the body both roles share — one library
manager, one exportarr sidecar, a config PVC at /config and the media tree at /data. Until
#2871 the two templates were 94 lines each and differed on 22 of them, every one of those 22
either a name derived from the app or a value the role holds in `defaults/main.yml`. Two copies
of a 94-line body is where a field drifts: the pod that gets the next probe tuning is whichever
one the author had open.

The guard is textual, and has two halves per caller, because a macro cannot force its own call:

  * the call is present — the `{% from %}` import and an `arr_deployment(` invocation.
  * no owned line is written out beside it — a `kind:`, a `containers:` or a `volumes:` in the
    caller means part of the body came back as a copy.

prowlarr is deliberately NOT a caller and is not checked here: no media mount, no DOCKER_MODS,
no startupProbe, plus a node affinity the other two do not carry. The macro's own header says
why that is a second shape rather than more arguments.

`test_workload_shell_uses_the_macros.py` is the other half — it scans the MACRO for the
`spec_shell`/`pod_shell` calls the callers no longer make themselves.

Run: uv run pytest ansible/tests/k8s/test_arr_deployments_share_one_macro.py
"""

import re

from _helpers import K8S_ROLES
from lib.repo_paths import SHARED_TPL

MACRO = SHARED_TPL / "arr-deployment.yml.j2"
# The roles whose deployment.yaml.j2 must be a call and nothing else. Named rather than
# discovered: a census that globbed for callers would go empty on a rename and pass on nothing.
CALLERS = ("radarr", "sonarr")

_IMPORT = "{% from 'arr-deployment.yml.j2' import arr_deployment with context %}"
_CALL = re.compile(r"\{\{\s*arr_deployment\(")
# A line of the body, at the depth the macro emits it. `kind:`/`apiVersion:` sit at column 0;
# `containers:`/`volumes:` at 6, under `spec.template.spec:`.
_COPIED_BODY = re.compile(
    r"^(?:(kind|apiVersion):|      (containers|volumes):)", re.MULTILINE
)


def call_offences(text: str) -> list[str]:
    """One line per way this template body is not just a call to the shared macro."""
    offences = []
    if _IMPORT not in text:
        offences.append("does not import arr_deployment")
    if not _CALL.search(text):
        offences.append("no arr_deployment() call")
    for m in _COPIED_BODY.finditer(text):
        key = m.group(2) or m.group(1)
        offences.append(f"`{key}:` written out instead of the macro")
    return offences


def test_the_macro_exists_and_emits_the_shared_body():
    text = MACRO.read_text()
    assert "{% macro arr_deployment(" in text
    # The two calls the macro makes on its callers' behalf, so a body that lost them fails here
    # as well as in the workload-shell census.
    assert "{{ spec_shell('Recreate') }}" in text
    assert "{{ pod_shell('homelab-standard') }}" in text


def test_both_arr_roles_are_only_a_call_to_the_macro():
    offenders = {}
    for role in CALLERS:
        template = K8S_ROLES / role / "templates/deployment.yaml.j2"
        assert template.exists(), f"{role} has no deployment template to check"
        if bad := call_offences(template.read_text()):
            offenders[role] = bad
    assert offenders == {}, (
        "radarr and sonarr must render from ansible/templates/arr-deployment.yml.j2: "
        f"{offenders}"
    )


_CLEAN = f"""\
{_IMPORT}
{{{{ arr_deployment('radarr',
                  image=radarr_k8s_image,
                  port=container_item.port) }}}}
"""


def test_a_call_only_template_is_clean():
    assert call_offences(_CLEAN) == []


def test_a_missing_call_or_a_copied_body_line_is_flagged():
    assert call_offences("---\nkind: Deployment\n") == [
        "does not import arr_deployment",
        "no arr_deployment() call",
        "`kind:` written out instead of the macro",
    ]
    copied = _CLEAN + "      containers:\n        - name: radarr\n"
    assert call_offences(copied) == ["`containers:` written out instead of the macro"]
