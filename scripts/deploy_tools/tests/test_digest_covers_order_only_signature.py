"""The `signature` rows the review of #5209's fix added, beside the red file at its line cap.

Each row is a write `signature` compares between the ends: a tuple, which no walk copies, and
a function swapped into a logger's filters, which leaves the list's length alone. The
#6425 and #6433 rows re-point a reference between two set items, and re-bind a method to
another handler.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers_order_only_signature.py
"""

import pytest

from test_digest_covers import tree  # noqa: F401
from test_digest_covers_order_only_unseen_state import (
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
