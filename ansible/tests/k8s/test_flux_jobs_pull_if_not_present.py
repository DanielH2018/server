"""The digest pull-policy row, run over the Jobs only the renderer emits.

`rendered_docs()` reads a role's top-level `templates/*.j2`. A role's `templates/verify/` and
`templates/precheck/` render only through `renderer_lib.cluster.render_tag`, into the
`<tag>-verify` and `<tag>-precheck` directories Flux applies, so the row in
`_image_property_rows.py` cannot see them. This module hands it that render (#6136).

Run: uv run pytest ansible/tests/k8s/test_flux_jobs_pull_if_not_present.py
"""

from dataclasses import replace

import pytest

from _helpers import K8S_ROLES
from _image_property_rows import IMAGE_PROPERTIES
from _property_table import check
from lib import yaml_fast
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

# 22 digest-pinned containers render across these Jobs; the floor is the count.
_FLOOR = 22

# A verify Job, a precheck Job, and registry's `flux_job_image` branch, which sits beside an
# `Always` in the other arm of an `{% if %}`.
_MUST_FIND = frozenset(
    {
        "crowdsec/job.yaml:verify",
        "pihole/guard.yaml:guard",
        "registry/job.yaml:verify",
        "traefik/bouncer-key.yaml:precheck",
    }
)


@pytest.fixture(scope="module")
def flux_job_docs(tmp_path_factory) -> list[tuple[str, str, dict]]:
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    stubs = load_stubs(path)
    roles = sorted(
        {p.parent.parent.name for p in K8S_ROLES.glob("*/templates/verify")}
        | {p.parent.parent.name for p in K8S_ROLES.glob("*/templates/precheck")}
    )
    docs = []
    for role in roles:
        rendered = cluster.render_tag(role, stubs)
        for name, text in {**rendered.verify, **rendered.precheck}.items():
            if name == "kustomization.yaml":
                continue
            for doc in yaml_fast.safe_load_all(text):
                if isinstance(doc, dict) and doc.get("kind"):
                    docs.append((role, name, doc))
    return docs


def test_digest_pinned_flux_job_containers_pull_if_not_present(flux_job_docs):
    (row,) = IMAGE_PROPERTIES

    # A rendered Job's name carries its spec digest, so the key names the template instead.
    def select(role: str, tpl: str, doc: dict):
        for key, container in row.select(role, tpl, doc):
            yield f"{role}/{tpl}:{key.rsplit(':', 1)[1]}", container

    problems = check(
        replace(row, select=select, min_matches=_FLOOR, must_find=_MUST_FIND),
        flux_job_docs,
    )
    assert not problems, "\n".join(problems)
