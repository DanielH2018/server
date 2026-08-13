#!/usr/bin/env python3
"""Drift guard for the hand-synced homepage services.yaml.j2 fork.

The k8s homepage role (E3, 2026-08-12) forked services.yaml.j2 from the Docker role
instead of lookup()-ing it like the other config files, because two Docker-only widget
keys (`server:`/`container:`) crash the k8s deployment's docker.yaml-less setup, and
peanut's widget URL had to move to the -k8s monitoring route. The fork's header comment
says "kept in sync by hand" — this test is that promise made executable: it normalizes
away exactly the documented deliberate differences and fails on anything else, so a tile
edited in one copy but not the other is caught in CI instead of appearing on one
dashboard and silently not the other.

Run: uv run pytest ansible/tests/test_homepage_services_fork_sync.py
"""

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[1]
DOCKER_TPL = (
    ANSIBLE / "roles" / "containers" / "homepage" / "templates" / "services.yaml.j2"
)
K8S_TPL = ANSIBLE / "roles" / "k8s" / "homepage" / "templates" / "services.yaml.j2"

# The one value rewritten in the fork: peanut's widget reaches the k8s monitoring route
# instead of the dead Docker-internal name.
DOCKER_PEANUT_URL = "url: http://peanut:8080"
K8S_PEANUT_URL = "url: https://peanut{{ k8s_hostname_suffix }}.local.{{ domain }}"


def normalize(text: str, *, drop_docker_keys: bool) -> list[str]:
    """Strip comments and the documented deliberate differences; keep everything else."""
    text = re.sub(r"\{#.*?#\}\n?", "", text, flags=re.DOTALL)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if drop_docker_keys and (
            stripped == "server: my-docker" or stripped.startswith("container: ")
        ):
            continue
        if stripped == K8S_PEANUT_URL:
            line = line.replace(K8S_PEANUT_URL, DOCKER_PEANUT_URL)
        lines.append(line)
    return lines


def test_fork_matches_docker_template_modulo_documented_differences():
    docker = normalize(DOCKER_TPL.read_text(), drop_docker_keys=True)
    fork = normalize(K8S_TPL.read_text(), drop_docker_keys=False)
    assert docker == fork, (
        "homepage services.yaml.j2 fork has drifted from the Docker template — "
        "mirror the edit in both roles/containers/homepage/templates/ and "
        "roles/k8s/homepage/templates/ (only server:/container: keys and the peanut "
        "widget URL may differ)"
    )


def test_the_deliberate_differences_are_still_present():
    """If the templates converge for real (e.g. Docker copy deleted or rewritten), the
    normalization above becomes dead logic — surface that instead of guarding nothing."""
    docker = DOCKER_TPL.read_text()
    fork = K8S_TPL.read_text()
    assert "server: my-docker" in docker and "server: my-docker" not in fork
    assert DOCKER_PEANUT_URL in docker and K8S_PEANUT_URL in fork
