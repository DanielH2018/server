"""Tests for validate.jinja_bash_collisions — the `${#` / `{#` collision scan.

Every rule is a `..._is_clean` / `..._is_flagged` pair, so a rule that silently stopped
matching fails its own test rather than reading green on an empty result.
"""

from validate import jinja_bash_collisions as v

# Two templates the census must contain, one per tree the scan has to reach. A glob that stops
# matching returns an empty set, and an `all(...)` over nothing passes — so the floor below is
# paired with names whose absence the failure can print.
CENSUS_MEMBERS = frozenset(
    {
        "ansible/roles/k8s/pihole/templates/configmap.yaml.j2",
        "ansible/templates/ingressroute.yml.j2",
    }
)


def test_a_plain_shell_template_is_clean():
    assert v.find_collisions('#!/bin/bash\nfor f in "$@"; do echo "${f}"; done\n') == []


def test_a_bash_length_expansion_is_flagged():
    found = v.find_collisions("#!/bin/bash\nif [ ${#args} -gt 0 ]; then echo hi; fi\n")
    assert found == [(2, "if [ ${#args} -gt 0 ]; then echo hi; fi")]


def test_a_length_expansion_inside_a_raw_block_is_clean():
    assert v.find_collisions("{% raw %}\necho ${#args}\n{% endraw %}\n") == []


def test_a_length_expansion_after_a_raw_block_closes_is_flagged():
    found = v.find_collisions("{% raw %}\necho ${#a}\n{% endraw %}\necho ${#b}\n")
    assert found == [(4, "echo ${#b}")]


def test_a_whitespace_controlled_raw_block_is_clean():
    assert v.find_collisions("{%- raw -%}\necho ${#args}\n{%- endraw -%}\n") == []


def test_an_unterminated_raw_block_covers_to_end_of_file():
    # Jinja's lexer swallows the rest of the file, so re-arming the rule here would report a
    # collision in text that never reaches the comment lexer.
    assert v.find_collisions("{% raw %}\necho ${#a}\necho ${#b}\n") == []


def test_blanking_a_raw_block_preserves_line_numbers():
    blanked = v.blank_raw_blocks("a\n{% raw %}\n${#x}\n{% endraw %}\nb\n")
    assert blanked.count("\n") == 5
    assert blanked.splitlines()[4] == "b"


def test_the_scan_finds_the_templates():
    """Without this, the live-tree test below passes vacuously on an empty glob."""
    found = {str(p.relative_to(v.REPO)) for p in v.templates()}
    assert CENSUS_MEMBERS <= found, f"census lost: {CENSUS_MEMBERS - found}"
    assert len(found) >= 400


def test_no_live_template_carries_the_collision():
    bad = {
        str(tpl.relative_to(v.REPO)): hits
        for tpl in v.templates()
        if (hits := v.find_collisions(tpl.read_text(errors="replace")))
    }
    assert not bad, f"bash length expansion in a Jinja template: {bad}"
