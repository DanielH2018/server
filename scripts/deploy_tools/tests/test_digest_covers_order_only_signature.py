"""The `signature` rows the review of #5209's fix added, beside the red file at its line cap.

Each row is a write `signature` compares between the ends: a tuple, which no walk copies, and
a function swapped into a logger's filters, which leaves the list's length alone.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers_order_only_signature.py
"""

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
