"""The `signature` rows the review of #5209's fix added, beside the red file at its line cap.

Each row is a write `signature` compares between the ends: a tuple, which no walk copies, and
a function swapped into a logger's filters, which leaves the list's length alone. The
#6425 and #6433 rows re-point a reference between two set items, and re-bind a method to
another handler. The #6807 row keeps two unchanged set items order-only when their hash order
swaps, and the #6809 row re-binds a method between two handlers reached earlier elsewhere.
The #6825 rows write alike at both ends to an object under a set item whose hash order swaps.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers_order_only_signature.py
"""

import pytest

import digest_covers

from test_digest_covers import _commit, tree  # noqa: F401
from test_digest_covers import TOPOSORT
from test_digest_covers_order_only_unseen_state import (
    _DEP_MAP_RETURN,
    _reason_for_head_call,
    _reason_for_head_module_code,
)


def test_a_tuple_the_module_code_writes_to_an_object_it_created_keeps_the_line(
    tree,  # noqa: F811
):
    """A tuple attribute of a module-created logger that the head lengthens keeps the line (#5209).

    No walk copies a tuple, so only `signature` recording its length and items tells the ends
    apart.
    """
    reason = _reason_for_head_module_code(
        tree,
        "import logging\n_LOGGER = logging.getLogger('order-only-5209')\n"
        "_LOGGER.order_only_levels = (10,)",
        "_LOGGER.order_only_levels = (10, 20)",
    )
    assert reason and "every deploy runs" in reason


_FILTERS = (
    "import logging\n\n\n"
    "def _keep(record):\n    return True\n\n\n"
    "def _drop(record):\n    return False"
)


def test_a_callable_filter_replaced_on_a_logger_the_chain_creates_keeps_the_line(
    tree,  # noqa: F811
):
    """A function filter the head swaps for another, on a logger the chain creates (#5209).

    The list keeps its length and no walk copies a function, so only `signature` naming the
    function tells the ends apart. A filter returning False drops every later record.
    """
    reason = _reason_for_head_call(
        tree,
        _FILTERS,
        "logging.getLogger('order-only-5209').filters[0] = _drop",
        base_call="logging.getLogger('order-only-5209').addFilter(_keep)",
    )
    assert reason and "every deploy runs" in reason


def test_a_callable_filter_left_alone_on_a_logger_the_chain_creates_is_order_only(
    tree,  # noqa: F811
):
    """The control for the row above: the same function filter at both ends discharges."""
    reason = _reason_for_head_call(
        tree,
        _FILTERS,
        "logging.getLogger('order-only-5209').filters[0] = _keep",
        base_call="logging.getLogger('order-only-5209').addFilter(_keep)",
    )
    assert reason is None


_HELD_IN_A_SET = (
    "import json\n\n\n"
    "class _Box:\n    pass\n\n\n"
    "_A = _Box()\n_A.name = 'a'\n_B = _Box()\n_B.name = 'b'\n"
    "json.order_only_6425 = {_A, _B}\n"
    "json.order_only_6425_holder = _Box()\n"
    "json.order_only_6425_holder.ref = _A"
)


def test_a_reference_re_pointed_between_two_objects_held_in_a_set_keeps_the_line(
    tree,  # noqa: F811
):
    """A module-created object's `ref` the head re-points from one set item to the other (#6425).

    Both items are walked under the set, whose item paths follow hash order, so `signature`
    must name each by something stable rather than writing the same `@~` for both.
    """
    reason = _reason_for_head_module_code(
        tree, _HELD_IN_A_SET, "json.order_only_6425_holder.ref = _B"
    )
    assert reason and "every deploy runs" in reason


_TWO_HANDLERS = (
    "_h1 = logging.Handler(logging.INFO)\n_h2 = logging.Handler(logging.CRITICAL)\n"
    "_h0 = logging.NullHandler()\nlogging.getLogger('order-only-6433').addHandler(_h0)"
)


@pytest.mark.parametrize(
    "base_call, call",
    [
        (
            "logging.getLogger('order-only-6433').addFilter(_h1.filter)",
            "logging.getLogger('order-only-6433').filters[0] = _h2.filter",
        ),
        (
            "logging.getLogger('order-only-6433').handlers[0].emit = _h1.emit",
            "logging.getLogger('order-only-6433').handlers[0].emit = _h2.emit",
        ),
    ],
    ids=["filter-re-bound", "emit-re-bound"],
)
def test_a_bound_method_re_bound_to_another_chain_created_handler_keeps_the_line(
    tree,  # noqa: F811
    base_call,
    call,
):
    """A bound method the head swaps for the same method of another handler (#6433).

    Both ends name `Filterer.filter` or `Handler.emit` alike, so only the method's
    `__self__`, h1 at INFO against h2 at CRITICAL, tells the ends apart.
    """
    reason = _reason_for_head_call(
        tree, "import logging", call, base_call=f"{_TWO_HANDLERS}\n{base_call}"
    )
    assert reason and "every deploy runs" in reason


# `_HASH` sits in the plugin's own module, which no walk reaches, so the head re-hashing and
# rebuilding the set changes only its iteration order, as a second process does.
_TWO_IN_A_SET = (
    "import json\n\n\n"
    "_HASH = {'a': 1, 'b': 2}\n\n\n"
    "class _Box:\n    def __hash__(self):\n        return _HASH[self.name]\n\n\n"
    "_A = _Box()\n_A.name = 'a'\n_B = _Box()\n_B.name = 'b'\n"
    "json.order_only_6807 = {_A, _B}"
)


def test_two_objects_created_in_a_set_unchanged_between_the_ends_is_order_only(
    tree,  # noqa: F811
):
    """Two module-created objects in a set whose hash order swaps between the ends (#6807).

    Neither object changes, so the `created` rows under the set must not name each by its
    hash-order index.
    """
    reason = _reason_for_head_module_code(
        tree,
        _TWO_IN_A_SET,
        "_HASH.update(a=2, b=1)\njson.order_only_6807 = {_A, _B}",
    )
    assert reason is None


def test_a_filter_re_bound_between_two_handlers_the_logger_also_holds_keeps_the_line(
    tree,  # noqa: F811
):
    """`filters[0]` swapped from h1.filter to h2.filter, both in the logger's handlers (#6809).

    Re-creating `filters` moves it after `handlers` in the logger's `__dict__`, so the
    breadth-first walk reaches both handlers before the method's `__self__` at both ends. The
    `__self__` must then name which handler it points at rather than write nothing.
    """
    handlers = (
        "_h1 = logging.Handler(logging.INFO)\n_h2 = logging.Handler(logging.CRITICAL)\n"
        "logging.getLogger('order-only-6809').addHandler(_h1)\n"
        "logging.getLogger('order-only-6809').addHandler(_h2)\n"
        "del logging.getLogger('order-only-6809').filters\n"
        "logging.getLogger('order-only-6809').filters = [_h1.filter]"
    )
    reason = _reason_for_head_call(
        tree,
        "import logging",
        "logging.getLogger('order-only-6809').filters[0] = _h2.filter",
        base_call=handlers,
    )
    assert reason and "every deploy runs" in reason


def test_a_set_item_created_by_the_module_code_that_changes_with_its_hash_order_keeps_the_line(
    tree,  # noqa: F811
):
    """One of two module-created set items changes while their hash order swaps (#6807).

    Dropping the index must leave each item's signature compared, not the set path's rows.
    `_HASH` gains `c`, or hashing `_B` raises and the run reads as doubt, which keeps the line too.
    """
    reason = _reason_for_head_module_code(
        tree,
        _TWO_IN_A_SET,
        "_HASH.update(a=2, b=1, c=3)\n_B.name = 'c'\njson.order_only_6807 = {_A, _B}",
    )
    assert reason and "every deploy runs" in reason


_TWO_IN_A_SET_WITH_A_LIST = (
    _TWO_IN_A_SET.replace("json.order_only_6807", "json.order_only_6825")
    + "\n_A.items = []\n_B.items = []"
)


@pytest.mark.parametrize(
    "call",
    ["_A.order_only_6825 = True", "_A.items.append(1)"],
    ids=["set-item-written", "list-under-a-set-item-written"],
)
def test_an_object_under_a_set_item_the_chain_writes_alike_at_both_ends_is_order_only(
    tree,  # noqa: F811
    call,
):
    """The dep map writes the same object under a set item at both ends, whose hash order swaps (#6825).

    The second walk copies `_A`, and its `items` list, under an index that follows hash
    order, so the changed-object rows must not name either by that index.
    """
    lines = "".join(f"    {line}\n" for line in call.split("\n"))
    plugin = f"{_TWO_IN_A_SET_WITH_A_LIST}\n\n\n{TOPOSORT}".replace(
        _DEP_MAP_RETURN, lines + _DEP_MAP_RETURN
    )
    tree.write("ansible/filter_plugins/toposort.py", plugin)
    base = tree.commit("toposort writes an object under a set item")
    swapped = f"{_TWO_IN_A_SET_WITH_A_LIST}\n_HASH.update(a=2, b=1)\n" + (
        "json.order_only_6825 = {_A, _B}"
    )
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": plugin.replace(
                    _TWO_IN_A_SET_WITH_A_LIST, swapped
                )
            }
        ],
    )
    assert digest_covers.Classifier(tree.root).reason("authelia", base, head) is None
