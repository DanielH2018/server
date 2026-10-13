"""The renderer's secret checks (target slice R2).

This module holds `PASSTHROUGH_FILTERS` only. R2's PR 2 adds the five rules that read it.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

# The repo filters that place an argument into their output unchanged. Rule 5 accepts a secret
# key as an argument to a member, and refuses it as an argument to any other filter, so the set
# is closed. Each member passes the membership test in
# `scripts/renderer/tests/test_renderer_secret_checks.py`, and a filter that fails it leaves
# the set.
PASSTHROUGH_FILTERS = frozenset(
    {
        # ansible/filter_plugins/homepage_tiles.py:homepage_href
        "homepage_href",
        # ansible/filter_plugins/authelia_access.py:authelia_service_rules
        "authelia_service_rules",
    }
)
