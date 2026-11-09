"""Every k8s render context resolves in `_resolve`'s whole-dict pass, not its per-key fallback.

One uncached build of the k8s render runs with spies on `render_context._resolve` and on the
`resolve_vars` it calls. The spies wrap the real functions, so the render itself is unchanged.

Run: uv run pytest ansible/tests/k8s/test_k8s_render_context_whole_dict_pass.py
"""

from dataclasses import dataclass, field

import pytest

import _k8s_render
from lib import render_context as rc

# Inventory keys that read a name only Ansible or a setup role defines (#7845).
ALWAYS_UNDEFINED = frozenset({"k8s_no_mutate", "claude_code_watch_uids"})


@dataclass
class Spied:
    """What one k8s render's `_resolve` calls returned, and what they cost."""

    resolves: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    resolve_vars_calls: int = 0


@pytest.fixture(scope="module")
def spied() -> Spied:
    seen = Spied()
    real_resolve, real_resolve_vars = rc._resolve, rc.resolve_vars

    def resolve(raw, *args, **kwargs):
        resolved, dropped = real_resolve(raw, *args, **kwargs)
        seen.resolves.append((len(raw), dict(dropped)))
        return resolved, dropped

    def resolve_vars(*args, **kwargs):
        seen.resolve_vars_calls += 1
        return real_resolve_vars(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rc, "_resolve", resolve)
        mp.setattr(rc, "resolve_vars", resolve_vars)
        # Uncached: `rendered_docs()` is prebuilt before any test runs, so a spy would see nothing.
        _k8s_render.render_texts({})
    assert len(seen.resolves) >= 100, "the spy saw too few contexts to judge the render"
    return seen


def test_no_k8s_render_context_drops_k8s_no_mutate_or_claude_code_watch_uids(spied):
    dropped = {
        key: reason
        for _, keys in spied.resolves
        for key, reason in keys.items()
        if key in ALWAYS_UNDEFINED
    }
    assert dropped == {}


def test_k8s_render_resolves_each_clean_context_in_the_whole_dict_pass(spied):
    # A context that drops a key outside ALWAYS_UNDEFINED (a role's built-image tag in stub
    # mode) may still take the per-key fallback, two rounds over its keys; any other gets 2.
    budget = sum(
        2 * size + 2 if set(dropped) - ALWAYS_UNDEFINED else 2
        for size, dropped in spied.resolves
    )
    calls = spied.resolve_vars_calls
    assert calls <= budget
