"""What one run of the vendored time-tagger writes to the container log, which Loki stores.

Most of these tests read the script through `ast`, like `test_time_tagger_wiring.py`, and
model what loguru and tqdm do with it:

- loguru starts with a default stderr handler at DEBUG, which only `logger.remove()` drops;
- the deployment runs `--reset_all=True` with `create_lists` left at its default, and stderr
  is not a TTY.

The first premise holds only under the environment the Deployment sets, so one test runs the
script's handler setup for real, with loguru, under that environment. Its other imports
(`tqdm`, `fire`, `bs4`, `karakeep_python_api`) are not in this repo's dev group, so the
driver stubs them, and its `KarakeepAPI` repeats what karakeep-python-api 1.9.1's
`_configure_logging` does to loguru's handlers.

Run: uv run pytest k8s/roles/karakeep/tests/test_time_tagger_logging.py
"""

import ast
import os
import re
import subprocess
import sys
from typing import TypeGuard
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1]
SCRIPT = ROLE / "files" / "karakeep-time-tagger.py"
DEPLOYMENT = ROLE / "templates" / "deployment-time-tagger.yaml.j2"

# Loaded as `tagger`, so its records are the script's, never the library's.
_DRIVER = """
import runpy, sys, types
from loguru import logger

for name, attr, value in (
    ("fire", "Fire", lambda f: None),
    ("tqdm", "tqdm", lambda it=None, **k: it),
    ("bs4", "BeautifulSoup", object),
):
    stub = types.ModuleType(name)
    setattr(stub, attr, value)
    sys.modules[name] = stub


def library_record(record):
    return (record["name"] or "").startswith("karakeep_python_api")


class KarakeepAPI:
    def __init__(self):
        try:
            logger.remove(0)
        except ValueError:
            pass
        else:
            logger.add(sys.stderr, level="DEBUG", filter=lambda r: not library_record(r))
        logger.add(sys.stderr, level="INFO", filter=library_record)


api = types.ModuleType("karakeep_python_api")
api.KarakeepAPI = KarakeepAPI
sys.modules["karakeep_python_api"] = api

script = runpy.run_path(sys.argv[1], run_name="tagger")
tagger = script["AddTimeToRead"]()
tagger.setup_logging(False)
KarakeepAPI()
bookmark = types.SimpleNamespace(content=types.SimpleNamespace(type="unsupported"))
tagger.extract_content_text(bookmark)
print("driver finished", file=sys.stderr)
"""

RUNS_PER_DAY = 96
DAILY_LINE_BUDGET = 1000
SHIPPED_LEVELS = {"info", "success", "warning"}
PER_BOOKMARK_METHODS = {"process_bookmark"}
FILTER = "not_library_record"


def _tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text())


def _class(tree: ast.Module) -> ast.ClassDef:
    return next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AddTimeToRead"
    )


def _method(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        n
        for n in _class(tree).body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _logger_call(node: ast.AST, method: str | None = None) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
        and (method is None or node.func.attr == method)
    )


def _is_stderr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "stderr"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _flag_branch(node: ast.If, flags: dict[str, bool]) -> list[ast.stmt] | None:
    """The branch a bare `if <flag>:` takes on the deployment's path, or None for other tests."""
    test = node.test
    negated = False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        negated = True
        test = test.operand
    if isinstance(test, ast.Name) and test.id in flags:
        return node.body if flags[test.id] != negated else node.orelse
    return None


DEPLOYED_FLAGS = {"reset_all": True, "create_lists": True, "verbose": False}


def _stderr_handlers_on_the_deployed_path(tree: ast.Module) -> list[str]:
    """The level of every stderr handler loguru holds once `setup_logging` has run."""
    handlers = ["DEBUG"]  # loguru's default handler

    def walk(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                branch = _flag_branch(stmt, DEPLOYED_FLAGS)
                walk(stmt.body + stmt.orelse if branch is None else branch)
                continue
            for node in ast.walk(stmt):
                if _logger_call(node, "remove") and not node.args:
                    handlers.clear()
                elif (
                    _logger_call(node, "add") and node.args and _is_stderr(node.args[0])
                ):
                    level = next(
                        (k.value for k in node.keywords if k.arg == "level"), None
                    )
                    handlers.append(
                        str(level.value) if isinstance(level, ast.Constant) else "DEBUG"
                    )

    walk([s for s in tree.body if not isinstance(s, ast.ClassDef | ast.FunctionDef)])
    walk(_method(tree, "setup_logging").body)
    return handlers


def _lines_one_run_ships(tree: ast.Module) -> int:
    """Upper bound on the INFO-or-higher lines `run` emits when no bookmark changes.

    An `except` body, a loop over bookmarks and `process_bookmark` fire only on an error or a
    changed bookmark, so they count zero; any other branch counts its larger arm, and a call to
    another `self.` method counts that method's lines.
    """
    methods = {n.name: n for n in _class(tree).body if isinstance(n, ast.FunctionDef)}

    def count(stmts: list[ast.stmt]) -> int:
        total = 0
        for stmt in stmts:
            if isinstance(stmt, ast.Try):
                total += count(stmt.body + stmt.orelse + stmt.finalbody)
            elif isinstance(stmt, ast.If):
                branch = _flag_branch(stmt, DEPLOYED_FLAGS)
                total += (
                    max(count(stmt.body), count(stmt.orelse))
                    if branch is None
                    else count(branch)
                )
            elif isinstance(stmt, ast.For | ast.While):
                target = stmt.target if isinstance(stmt, ast.For) else None
                if not (isinstance(target, ast.Name) and target.id == "bookmark"):
                    total += count(stmt.body)
            elif isinstance(stmt, ast.With):
                total += count(stmt.body)
            else:
                for node in ast.walk(stmt):
                    if (
                        _logger_call(node)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in SHIPPED_LEVELS
                    ):
                        total += 1
                    elif (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr in methods
                        and node.func.attr not in PER_BOOKMARK_METHODS
                    ):
                        total += count(methods[node.func.attr].body)
        return total

    return count(methods["run"].body)


def test_the_default_debug_handler_is_removed_so_debug_lines_do_not_ship():
    handlers = _stderr_handlers_on_the_deployed_path(_tree())
    assert "DEBUG" not in handlers, (
        f"stderr carries handlers at {handlers}: loguru's default DEBUG handler survives "
        "setup_logging, so every bookmark's DEBUG line reaches Loki. Call logger.remove() "
        "before adding the INFO handler."
    )


def _deployment_env() -> dict[str, str]:
    pairs = re.findall(
        r"- name: (\w+)\n\s+value: \"?([^\"\n{]*)\"?\n", DEPLOYMENT.read_text()
    )
    return {name: value for name, value in pairs if name.startswith("LOGURU_")}


def test_under_the_deployed_env_no_script_debug_line_survives_the_library_setup():
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGURU_")}
    env |= _deployment_env()
    run = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "driver finished" in run.stderr, run.stderr
    assert "Unsupported content type" not in run.stderr, (
        f"with {_deployment_env() or 'no LOGURU_* env'}, a script DEBUG line reaches stderr "
        "after KarakeepAPI() sets up its logging: the library's logger.remove(0) took the "
        "script's INFO handler and left a DEBUG catch-all"
    )


def test_every_info_line_ships_once():
    handlers = _stderr_handlers_on_the_deployed_path(_tree())
    assert len(handlers) == 1, (
        f"stderr carries {len(handlers)} handlers ({handlers}) on the deployed path, so each "
        "INFO line is written that many times"
    )


def test_no_progress_bar_is_drawn_into_a_non_tty_log():
    """Zero tqdm calls passes too: a fix that drops tqdm draws no bar."""
    bars = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "tqdm"
    ]
    undisabled = [
        bar.lineno
        for bar in bars
        if not any(
            k.arg == "disable"
            and not (isinstance(k.value, ast.Constant) and k.value.value is False)
            for k in bar.keywords
        )
    ]
    assert not undisabled, (
        f"tqdm at lines {undisabled} draws a progress bar whatever stderr is; pass disable= "
        "so a non-TTY log gets no carriage-return lines"
    )


def test_no_file_sink_accumulates_rotated_logs():
    unbounded = [
        n.lineno
        for n in ast.walk(_tree())
        if _logger_call(n, "add")
        and n.args
        and not _is_stderr(n.args[0])
        and not any(k.arg == "retention" for k in n.keywords)
    ]
    assert not unbounded, (
        f"logger.add at lines {unbounded} writes a file sink with no retention, so rotated "
        "files pile up in the pod's working directory"
    )


def test_one_run_logs_one_summary_line():
    lines = _lines_one_run_ships(_tree())
    assert lines == 1, (
        f"a run with no bookmark to change ships up to {lines} INFO-or-higher lines; it "
        "should ship one summary line"
    )


def test_a_day_of_runs_stays_under_the_loki_line_budget():
    lines = _lines_one_run_ships(_tree())
    handlers = len(_stderr_handlers_on_the_deployed_path(_tree()))
    per_day = lines * handlers * RUNS_PER_DAY
    assert 0 < per_day < DAILY_LINE_BUDGET, (
        f"{lines} lines x {handlers} stderr handlers x {RUNS_PER_DAY} runs = {per_day} lines "
        f"a day, over the {DAILY_LINE_BUDGET}-line budget"
    )


def test_every_slot_the_estimate_returns_is_a_tag_the_script_recognises():
    """A slot missing from TIME_TAGS reads as no tag at all, so every bookmark in it is
    re-tagged, with an INFO line and an API write, on every run."""
    tree = _tree()
    recognised = next(
        ast.literal_eval(n.value)
        for n in _class(tree).body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "TIME_TAGS" for t in n.targets)
    )
    returned = {
        str(r.value.value)
        for r in ast.walk(_method(tree, "estimate_reading_time"))
        if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)
    }
    assert {"0-5m", "30m+"} <= returned, returned
    assert returned <= set(recognised), (
        f"estimate_reading_time returns {sorted(returned - set(recognised))}, which "
        "TIME_TAGS does not list, so those bookmarks are re-tagged on every run"
    )


def _filter_function():
    """`not_library_record`, compiled alone: it needs none of the script's imports."""
    node = next(
        n for n in _tree().body if isinstance(n, ast.FunctionDef) and n.name == FILTER
    )
    namespace: dict = {}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT), "exec"),
        namespace,
    )
    return namespace[FILTER]


def test_the_filter_drops_the_library_records_and_keeps_the_scripts_own():
    """karakeep_python_api prints its records through its own sink, so a script handler
    that kept them would ship each twice; one that dropped `__main__` would ship nothing."""
    keep = _filter_function()
    assert not keep({"name": "karakeep_python_api.karakeep_api"}), (
        f"{FILTER} keeps a karakeep_python_api record, so the library's lines ship twice"
    )
    assert keep({"name": "__main__"}), (
        f"{FILTER} drops a __main__ record, so the script's own lines never ship"
    )


def test_every_stderr_handler_setup_logging_adds_uses_the_filter():
    adds = [
        n
        for n in ast.walk(_method(_tree(), "setup_logging"))
        if _logger_call(n, "add") and n.args and _is_stderr(n.args[0])
    ]
    filters = [
        next((ast.unparse(k.value) for k in n.keywords if k.arg == "filter"), None)
        for n in adds
    ]
    assert filters and all(f == FILTER for f in filters), (
        f"setup_logging adds stderr handlers with filter= {filters}; each needs "
        f"filter={FILTER}, which drops the library's records"
    )
