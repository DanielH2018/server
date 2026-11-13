"""A notify call is the last state-relevant step in any sequence that makes one.

A re-paired phone or watch renames its `notify.mobile_app_*` service. Calling the old name
raises ServiceNotFound, and HA re-raises that class even under `continue_on_error`, so the
sequence stops at the notify and every step after it is skipped. `script.bedroom_notify`
propagates the same abort to its caller. So a fan, light or persistent-note step placed after
a notify silently stops running on the day the slug changes; the watch slug changed once
already, on 2026-06-20. This test pins the order: in every automation and script, including
bedroom_notify's own fallback note, nothing but further notifies after a notify.
"""

from pathlib import Path

import yaml

FILES = Path(__file__).resolve().parent.parent / "files"

NOTIFY_PREFIXES = ("script.bedroom_notify", "notify.mobile_app_")

# Aborts on purpose: a failed digest push must leave the held notes undismissed.
ABORT_BY_DESIGN = {"bedroom_flush_held_notifications"}

# Callers that run a state change and a notify in one sequence; the census must reach each.
KNOWN_CALLERS = {
    "bedroom_arrive_home",
    "bedroom_fallback_wake",
    "bedroom_threshold_alert",
    "bedroom_notify",
}


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _items() -> dict[str, list]:
    items = {}
    for path in sorted((FILES / "automations").glob("*.yaml")):
        for automation in yaml.load(path.read_text(), Loader=_Loader) or []:
            items[automation["id"]] = automation.get("action") or automation.get(
                "actions"
            )
    for path in sorted((FILES / "scripts").glob("*.yaml")):
        for name, script in (yaml.load(path.read_text(), Loader=_Loader) or {}).items():
            items[name] = script.get("sequence")
    return items


def _service(step: dict) -> str:
    return str(step.get("service") or step.get("action") or "")


def _is_notify(step: dict) -> bool:
    return _service(step).startswith(NOTIFY_PREFIXES)


def _nested_lists(step: dict) -> list[list]:
    lists = [
        step.get(key) for key in ("then", "else", "sequence", "default", "parallel")
    ]
    lists += [option.get("sequence") for option in step.get("choose") or []]
    if isinstance(step.get("repeat"), dict):
        lists.append(step["repeat"].get("sequence"))
    return [steps for steps in lists if isinstance(steps, list)]


def _leaf_steps(step: dict) -> list[dict]:
    nested = _nested_lists(step)
    if not nested:
        return [step]
    return [leaf for steps in nested for child in steps for leaf in _leaf_steps(child)]


def _only_notifies(step: dict) -> bool:
    return all(_is_notify(leaf) or "stop" in leaf for leaf in _leaf_steps(step))


def _violations(steps: list, where: str) -> list[str]:
    found = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if _is_notify(step):
            for later in steps[index + 1 :]:
                if isinstance(later, dict) and not _only_notifies(later):
                    found.append(
                        f"{where}[{index}] {_service(step)} is followed by {later}"
                    )
        for nested in _nested_lists(step):
            found += _violations(nested, f"{where}[{index}]")
    return found


def _calls_notify(steps: list) -> bool:
    return any(
        _is_notify(leaf)
        for step in steps
        if isinstance(step, dict)
        for leaf in _leaf_steps(step)
    )


def test_no_state_change_follows_a_notify():
    items = _items()
    callers = {name for name, steps in items.items() if steps and _calls_notify(steps)}
    assert KNOWN_CALLERS <= callers, KNOWN_CALLERS - callers
    violations = [
        line
        for name, steps in items.items()
        if name not in ABORT_BY_DESIGN and steps
        for line in _violations(steps, name)
    ]
    assert not violations, "\n".join(violations)
