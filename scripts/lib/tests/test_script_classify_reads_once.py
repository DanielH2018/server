"""`classify` parses each script once per reader and walks it once per walking reader.

Three readers need a script's syntax tree: the import graph, the scripts it names, and its
module-level shape. A `classify()` of the real tree parsed each Python source up to six times
and walked it up to five, about 7s that two test modules paid (#5564). The verdicts do not
depend on how often a file is read, so only a count can see a repeat creep back.

Run: uv run pytest scripts/lib/tests/test_script_classify_reads_once.py
"""

import ast
from collections import Counter

from lib import script_classify as sc

# The counters key on source text, so every fixture file's text is its own.
GUARDED = 'def main():\n    return 0\nif __name__ == "__main__":\n    main()\n'


def _repo(tmp_path):
    """A gate that dispatches to `target.py`, which imports `helper.py` and names `tool.py`.

    `target.py` is the file every reader reaches: it is imported, it is a dispatch target
    with a `main` and no guard, and it invokes a script by its path.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gated.py").write_text('"""Summary."""\nimport target\n')
    (scripts / "target.py").write_text(
        '"""Summary."""\nimport helper\nTOOL = "scripts/tool.py"\n'
        "def main(argv=None):\n    return 0\n"
    )
    (scripts / "helper.py").write_text('"""Helper."""\n' + GUARDED)
    (scripts / "tool.py").write_text('"""Tool."""\n' + GUARDED)
    (tmp_path / "prek.toml").write_text('entry = "uv run python scripts/gated.py"\n')
    return tmp_path, scripts


def test_each_script_is_parsed_three_times_and_walked_twice_at_most(
    tmp_path, monkeypatch
):
    repo, scripts = _repo(tmp_path)
    parses: Counter[str] = Counter()
    walks: Counter[str] = Counter()
    sources: dict[int, tuple[ast.AST, str]] = {}
    real_parse, real_walk = ast.parse, ast.walk

    def parse(source, *args, **kwargs):
        parses[source] += 1
        tree = real_parse(source, *args, **kwargs)
        sources[id(tree)] = (tree, source)
        return tree

    def walk(node):
        if id(node) in sources:
            walks[sources[id(node)][1]] += 1
        return real_walk(node)

    monkeypatch.setattr(ast, "parse", parse)
    monkeypatch.setattr(ast, "walk", walk)
    verdicts = sc.classify(repo, scripts)

    # The fixture still reaches every reader: the dispatch rule credits `helper.py` and the
    # path literal credits `tool.py` with the gate's kind.
    assert verdicts["helper.py"][0] == verdicts["tool.py"][0] == "gate", verdicts
    target = (scripts / "target.py").read_text()
    assert parses[target] <= 3, f"target.py parsed {parses[target]} times"
    assert walks[target] <= 2, f"target.py walked {walks[target]} times"
    assert max(parses.values()) <= 3, parses
    assert max(walks.values()) <= 2, walks
