"""Unit tests for the runtime-error-alert scope macro in custom_templates/diagnostics.jinja."""

from jinja_harness import render_macro

DIAG = "diagnostics.jinja"


DISMISS_LOGGER = (
    "homeassistant.components.automation"
    ".bedroom_display_go_ambient_when_dark_or_sleeping"
)
# The two records a failed Cast turn_off logged live on 2026-11-12.
CAST_STEP_ERROR = (
    "Bedroom display go ambient when dark or sleeping: Error executing script. Error for "
    "call_service at pos 1: CastMediaPlayerEntity.turn_off Failed: Execution of quit app "
    "timed out after 10.0 s."
)
CAST_AUTOMATION_ERROR = (
    "Error while executing automation automation.bedroom_display_go_ambient_when_dark_or_"
    "sleeping: CastMediaPlayerEntity.turn_off Failed: Failed to execute quit app."
)


def _scope(level, logger, message=""):
    return render_macro(DIAG, "error_in_scope", level, logger, message)


def test_error_in_scope_our_code_errors_alert():
    assert (
        _scope("ERROR", "homeassistant.components.automation.bedroom_presence_on")
        == "True"
    )
    assert (
        _scope("CRITICAL", "homeassistant.components.script.bedroom_apply_natural")
        == "True"
    )
    assert _scope("ERROR", "homeassistant.components.template") == "True"
    assert _scope("ERROR", "homeassistant.helpers.template") == "True"
    assert _scope("ERROR", "homeassistant.components.shell_command") == "True"


def test_error_in_scope_excludes_warnings_and_info():
    assert _scope("WARNING", "homeassistant.components.automation.x") == "False"
    assert _scope("INFO", "homeassistant.components.automation.x") == "False"


def test_error_in_scope_excludes_third_party_loggers():
    # The benign HACS noise class — an ERROR, but not our code.
    assert _scope("ERROR", "custom_components.adaptive_lighting.switch") == "False"
    assert _scope("ERROR", "homeassistant.components.dreo") == "False"


def test_error_in_scope_tolerates_missing_logger():
    assert _scope("ERROR", None) == "False"


def test_error_in_scope_excludes_a_cast_device_fault():
    assert _scope("ERROR", DISMISS_LOGGER, CAST_STEP_ERROR) == "False"
    assert _scope("ERROR", DISMISS_LOGGER, CAST_AUTOMATION_ERROR) == "False"


def test_error_in_scope_keeps_our_own_failed_call():
    message = "Bedroom fan: Error executing script. Error for call_service at pos 2: Failed: x"
    assert (
        _scope("ERROR", "homeassistant.components.automation.bedroom_fan", message)
        == "True"
    )


ALARM = 1793793600.0


def _overdue(alarm_ts, now_ts):
    return render_macro(DIAG, "alarm_overdue_hours", alarm_ts, now_ts)


def test_alarm_overdue_hours_counts_hours_past_the_alarm():
    assert _overdue(ALARM, ALARM + 37 * 3600) == "37.0"
    assert _overdue(ALARM, ALARM + 90) == "0.0"


def test_alarm_overdue_hours_is_zero_for_a_future_or_current_alarm():
    assert _overdue(ALARM, ALARM - 3600) == "0"
    assert _overdue(ALARM, ALARM) == "0"


def test_alarm_overdue_hours_is_zero_without_an_alarm():
    assert _overdue(None, ALARM) == "0"
