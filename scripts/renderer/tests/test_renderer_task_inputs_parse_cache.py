"""`task_inputs.image_builder_sites` parses each tasks file once per content (#5900).

Each check profiles the calls with cProfile, as the issue measured them, and counts PyYAML's
`yaml.load`, which every parse path in the renderer reaches. Each role is a throwaway one
whose files carry a unique marker, so a parse cached by an earlier test cannot zero a count.

Run: uv run pytest scripts/renderer/tests/test_renderer_task_inputs_parse_cache.py
"""

import cProfile
import uuid
from pathlib import Path

import yaml

from renderer.renderer_lib import task_inputs

_YAML_LOAD = yaml.load.__code__


def _tasks_file(image: str) -> str:
    return (
        f"# {uuid.uuid4()}\n"
        "- name: Build the image\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/image-builder\n"
        "  vars:\n"
        f"    image_builder_name: {image}\n"
    )


def _role(tmp_path: Path, files: dict[str, str]) -> Path:
    tasks = tmp_path / "role" / "tasks"
    tasks.mkdir(parents=True)
    for name, text in files.items():
        (tasks / name).write_text(text)
    return tasks.parent


def _profiled(calls):
    """Each call's result, and how many times the calls parsed YAML between them."""
    profile = cProfile.Profile()
    results = [profile.runcall(call) for call in calls]
    parses = sum(
        entry.callcount for entry in profile.getstats() if entry.code is _YAML_LOAD
    )
    return results, parses


def _names(sites) -> list[str]:
    return [site.vars["image_builder_name"] for site in sites]


def test_repeat_calls_parse_each_tasks_file_once(tmp_path):
    role = _role(tmp_path, {"a.yml": _tasks_file("one"), "b.yml": _tasks_file("two")})

    (first, second), parses = _profiled(
        [lambda: task_inputs.image_builder_sites(role)] * 2
    )

    assert _names(first) == _names(second) == ["one", "two"]
    assert parses == 2, (
        f"two tasks files, two calls: parsed {parses} times, not once each"
    )


def test_an_edit_between_calls_is_seen_and_parsed_once(tmp_path):
    role = _role(tmp_path, {"main.yml": _tasks_file("before")})
    task_inputs.image_builder_sites(role)
    (role / "tasks" / "main.yml").write_text(_tasks_file("after"))

    (edited, again), parses = _profiled(
        [lambda: task_inputs.image_builder_sites(role)] * 2
    )

    assert _names(edited) == _names(again) == ["after"]
    assert parses == 1, f"one edited content: parsed {parses} times, not once"
