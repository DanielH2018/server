"""`test_smtp_wiring.py`'s docstrings name the receiver that sends with the credential (#7316).

The receiver's name comes from the rendered Alertmanager config, so a rename fails here. It is
matched as a whole word because the module docstring also holds `email_backstop`.
"""

import ast
import re

from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import REPO

SMTP_WIRING = REPO / "ansible/tests/services/test_smtp_wiring.py"


def email_receiver_name() -> str:
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    config = yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])
    names = [r["name"] for r in config["receivers"] if r.get("email_configs")]
    assert len(names) == 1, names
    return names[0]


def assert_names_the_email_receiver(doc: str | None) -> None:
    assert doc, "docstring is missing"
    assert "Alertmanager" in doc
    assert re.search(rf"\b{email_receiver_name()}\b", doc), doc


def test_module_docstring_names_alertmanagers_email_receiver():
    assert_names_the_email_receiver(
        ast.get_docstring(ast.parse(SMTP_WIRING.read_text()))
    )


def test_starttls_test_docstring_names_alertmanagers_email_receiver():
    tree = ast.parse(SMTP_WIRING.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_starttls_address_is_flagged"
    )
    assert_names_the_email_receiver(ast.get_docstring(function))
