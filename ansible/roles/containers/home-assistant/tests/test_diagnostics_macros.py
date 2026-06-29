"""Unit tests for the runtime-error-alert scope macro in custom_templates/diagnostics.jinja."""
from jinja_harness import render_macro

DIAG = "diagnostics.jinja"


def _scope(level, logger):
    return render_macro(DIAG, "error_in_scope", level, logger)


def test_error_in_scope_our_code_errors_alert():
    assert _scope("ERROR", "homeassistant.components.automation.bedroom_presence_on") == "True"
    assert _scope("CRITICAL", "homeassistant.components.script.bedroom_apply_natural") == "True"
    assert _scope("ERROR", "homeassistant.components.template") == "True"
    assert _scope("ERROR", "homeassistant.helpers.template") == "True"


def test_error_in_scope_excludes_warnings_and_info():
    assert _scope("WARNING", "homeassistant.components.automation.x") == "False"
    assert _scope("INFO", "homeassistant.components.automation.x") == "False"


def test_error_in_scope_excludes_third_party_loggers():
    # The benign HACS noise class — an ERROR, but not our code.
    assert _scope("ERROR", "custom_components.adaptive_lighting.switch") == "False"
    assert _scope("ERROR", "homeassistant.components.dreo") == "False"


def test_error_in_scope_tolerates_missing_logger():
    assert _scope("ERROR", None) == "False"
