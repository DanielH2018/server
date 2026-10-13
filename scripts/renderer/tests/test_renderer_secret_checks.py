"""`PASSTHROUGH_FILTERS` and the membership test each member passes (target slice R2).

The membership test calls a filter as a template does, once with the sentinel `r2-sentinel`
for the secret key and once with `a.b-c.example`. The first output, serialised as JSON, must
hold the sentinel, and replacing the sentinel there with `a.b-c.example` must give the second
output exactly.

Run: uv run pytest scripts/renderer/tests/test_renderer_secret_checks.py
"""

import json

import pytest

from authelia_access import FilterModule as AutheliaFilters
from homepage_tiles import FilterModule as HomepageFilters
from lib.repo_paths import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib.context import play_context
from renderer.renderer_lib.secret_checks import PASSTHROUGH_FILTERS

SENTINEL = "r2-sentinel"
OTHER = "a.b-c.example"
# authelia's templates/config-secret.yaml.j2 binds this list with `{% set %}` before its call.
RFC1918_NETWORKS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
# The filters as the templates reach them, so a member must be registered under its name.
FILTERS = {**HomepageFilters().filters(), **AutheliaFilters().filters()}

# Every call shape the templates write, with the secret key `domain` passed as `key`.
CALLS = {
    "homepage_href": [
        lambda f, entries, key: f(entries, "headlamp", key),
        lambda f, entries, key: f(entries, "pihole", key, "/admin"),
        lambda f, entries, key: f(entries, "longhorn-ui", key, lan=True),
    ],
    "authelia_service_rules": [
        lambda f, entries, key: f(entries, key, RFC1918_NETWORKS),
    ],
}


@pytest.fixture(scope="module")
def containers_list():
    return play_context(K8S_ROLES / "homepage", shadow_diff.stub_vars())[
        "containers_list"
    ]


def check_membership(call):
    """Raise AssertionError unless `call(key)` places `key` into its output unchanged."""
    with_sentinel = json.dumps(call(SENTINEL))
    assert SENTINEL in with_sentinel, (
        f"the output {with_sentinel} does not hold the sentinel {SENTINEL!r}"
    )
    replaced = with_sentinel.replace(SENTINEL, OTHER)
    expected = json.dumps(call(OTHER))
    assert replaced == expected, (
        f"replacing the sentinel gives {replaced}, but the call with {OTHER!r} gives {expected}"
    )


def test_every_member_has_its_template_calls():
    assert set(CALLS) == PASSTHROUGH_FILTERS


@pytest.mark.parametrize(
    ("name", "shape"),
    [(name, shape) for name, shapes in CALLS.items() for shape in range(len(shapes))],
)
def test_membership_of_each_member_is_clean(containers_list, name, shape):
    check_membership(
        lambda key: CALLS[name][shape](FILTERS[name], containers_list, key)
    )


@pytest.mark.parametrize(
    ("transform", "message"),
    [
        (str.upper, "does not hold the sentinel"),
        (lambda key: key.replace(".", "-"), "replacing the sentinel gives"),
    ],
)
def test_membership_of_a_transforming_filter_is_flagged(transform, message):
    def fixture_filter(entries, name, domain):
        return f"https://{name}.{transform(domain)}/"

    with pytest.raises(AssertionError, match=message):
        check_membership(lambda key: fixture_filter([], "headlamp", key))
