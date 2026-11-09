from lib.summary import grouped


def test_a_big_group_is_counted_and_a_small_one_named():
    pairs = [(f"svc{i}", "at abc, before this merge") for i in range(5)]
    pairs.append(("svc9", "not Ready"))
    assert (
        grouped(pairs, noun="Kustomization")
        == "5 Kustomizations at abc, before this merge; not Ready: svc9"
    )


def test_a_group_at_the_limit_is_named():
    pairs = [("a", "absent"), ("b", "absent"), ("c", "absent")]
    assert grouped(pairs, noun="Kustomization") == "absent: a, b, c"


def test_counting_every_group_joins_them_with_commas_largest_first():
    pairs = [("x", "skipped"), ("a", "healthy"), ("b", "healthy")]
    assert grouped(pairs, named_up_to=0) == "2 healthy, 1 skipped"


def test_no_pairs_is_an_empty_line():
    assert grouped([]) == ""
