from dev.duplicate_tests import duplicate_groups, tracked_test_sources

_COPY = '''
def test_the_total_includes_tax(cart):
    """Docstring {n}."""
    cart.add("book")  # comment {n}
    assert cart.total() == 11
'''

_OTHER = """
def test_the_total_excludes_shipping(cart):
    cart.add("book")
    assert cart.total() == 10
"""


def test_two_tests_with_one_body_are_flagged() -> None:
    sources = {
        "a/test_cart.py": _COPY.format(n=1),
        "b/test_checkout.py": "class TestTotals:\n"
        + _COPY.format(n=2).replace("\n", "\n    "),
    }
    assert duplicate_groups(sources) == [
        [
            "a/test_cart.py::test_the_total_includes_tax",
            "b/test_checkout.py::TestTotals::test_the_total_includes_tax",
        ]
    ]


def test_tests_that_differ_in_a_value_or_an_argument_are_clean() -> None:
    sources = {
        "a/test_cart.py": _COPY.format(n=1) + _OTHER,
        "b/test_checkout.py": _COPY.format(n=2).replace("(cart)", "(cart, tmp_path)"),
    }
    assert duplicate_groups(sources) == []


def test_one_body_parametrized_over_different_values_is_clean() -> None:
    sources = {
        "a/test_cart.py": '@pytest.mark.parametrize("cart", [1])' + _COPY.format(n=1),
        "b/test_checkout.py": '@pytest.mark.parametrize("cart", [2])'
        + _COPY.format(n=2),
    }
    assert duplicate_groups(sources) == []


def test_a_one_statement_body_is_not_a_copy() -> None:
    one_line = "def test_it_holds():\n    assert CHECK()\n"
    assert duplicate_groups({"a/test_a.py": one_line, "b/test_b.py": one_line}) == []


def test_the_census_reads_tracked_test_modules_only() -> None:
    sources = tracked_test_sources(["scripts/dev"])
    assert "scripts/dev/tests/ci/test_duplicate_tests.py" in sources
    assert "scripts/dev/duplicate_tests.py" not in sources
