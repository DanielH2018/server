"""The held-alert store over generated sequences of `bedroom_held_alert` events.

`sensor.bedroom_held_alerts` stores `held_after(held, event) | from_json` as its `held`
attribute and passes that attribute back in on the next event. The machine replays that loop
against a plain dict, starting from each value the attribute has before its first event.

Run: uv run pytest k8s/roles/home-assistant/tests/test_notify_properties.py
"""

import json

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule
from jinja_harness import render_macro

NOTIFY = "notify.jinja"

tags = st.sampled_from(["window_advice", "update_digest", "co2", "a-b.c"])
texts = st.text("abc :,'é", max_size=6)


class HeldStore(RuleBasedStateMachine):
    """The sensor's `held` attribute beside the dict it should equal."""

    def __init__(self):
        super().__init__()
        self.model = {}

    @initialize(held=st.sampled_from([None, "unknown", [], {}]))
    def sensor_starts_without_a_mapping(self, held):
        self.held = held

    def send(self, event):
        self.held = json.loads(render_macro(NOTIFY, "held_after", self.held, event))
        assert self.held == self.model
        digest = render_macro(NOTIFY, "held_digest", self.held)
        lines = [f"• {a['title']}: {a['message']}" for a in self.model.values()]
        assert digest == "\n".join(lines).strip()

    @rule(tag=tags, title=texts, message=texts)
    def hold(self, tag, title, message):
        self.model[tag] = {"title": title, "message": message}
        self.send({"action": "hold", "tag": tag, "title": title, "message": message})

    @rule(tag=tags)
    def cancel(self, tag):
        self.model.pop(tag, None)
        self.send({"action": "cancel", "tag": tag})

    @rule(delivered=st.lists(tags, max_size=3))
    def flushed(self, delivered):
        for tag in delivered:
            self.model.pop(tag, None)
        self.send({"action": "flushed", "tags": delivered})

    @rule(action=st.sampled_from(["", "unknown_action"]))
    def other_event(self, action):
        self.send({"action": action, "tag": "window_advice"})


TestHeldStore = HeldStore.TestCase
