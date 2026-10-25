"""The discord-relay sidecar's delivery loop and its listener (#5657).

The transport, the clock and the sleep are injected, so no test reaches Discord or waits.
"""

import http.client
import http.server
import threading
from pathlib import Path

import discord_relay

DROP = "Cannot send notification to Homelab Alerts"


class Fake:
    """A scripted Discord: each send pops the next (status, headers, body) answer."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []
        self.slept = []
        self.now = 0.0
        self.lines = []

    def send(self, url, body, content_type):
        self.sent.append((url, body, content_type))
        return self.answers.pop(0)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def relay(
        self, tmp_path: Path, deadline=900.0, max_queue=10
    ) -> discord_relay.Relay:
        url_file = tmp_path / "url"
        url_file.write_text("https://discord.example/hook\n")
        return discord_relay.Relay(
            url_file=str(url_file),
            max_queue=max_queue,
            deadline=deadline,
            send=self.send,
            sleep=self.sleep,
            clock=lambda: self.now,
            log=self.lines.append,
        )


def test_a_429_is_retried_after_discords_retry_after(tmp_path):
    fake = Fake([(429, {}, b'{"retry_after": 0.3, "global": false}'), (204, {}, b"")])
    relay = fake.relay(tmp_path)
    assert relay.accept(b'{"embeds": []}', "application/json")
    assert relay.run_once(timeout=0)
    assert fake.slept == [0.3]
    assert [s[1] for s in fake.sent] == [b'{"embeds": []}'] * 2
    assert fake.sent[0][0] == "https://discord.example/hook"
    assert not any(DROP in line for line in fake.lines)


def test_an_emptied_bucket_holds_the_next_send(tmp_path):
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "1.5"}
    fake = Fake([(204, headers, b"")])
    relay = fake.relay(tmp_path)
    relay.accept(b"{}", "application/json")
    relay.run_once(timeout=0)
    assert fake.slept == [1.5]


def test_a_refused_message_is_logged_as_a_kuma_drop(tmp_path):
    fake = Fake([(400, {}, b'{"message": "Cannot send an empty message"}')])
    relay = fake.relay(tmp_path)
    relay.accept(b"{}", "application/json")
    relay.run_once(timeout=0)
    assert len(fake.sent) == 1
    assert [line for line in fake.lines if DROP in line], fake.lines


def test_a_message_still_limited_at_its_deadline_is_logged_as_a_drop(tmp_path):
    fake = Fake([(429, {}, b'{"retry_after": 40}')] * 10)
    relay = fake.relay(tmp_path, deadline=100.0)
    relay.accept(b"{}", "application/json")
    relay.run_once(timeout=0)
    assert fake.slept == [40, 40]
    assert [line for line in fake.lines if DROP in line and "gave up" in line], (
        fake.lines
    )


def test_a_full_queue_refuses_so_kuma_logs_the_drop_itself(tmp_path):
    relay = Fake([]).relay(tmp_path, max_queue=1)
    assert relay.accept(b"{}", "application/json")
    assert not relay.accept(b"{}", "application/json")


def test_stopping_logs_one_drop_per_undelivered_message(tmp_path):
    fake = Fake([])
    relay = fake.relay(tmp_path)
    relay.accept(b"{}", "application/json")
    relay.accept(b"{}", "application/json")
    assert relay.abandon() == 2
    assert sum(DROP in line for line in fake.lines) == 2


def test_the_listener_answers_before_discord_does(tmp_path):
    """Kuma awaits the send inside the push route, so a held answer stalls every pusher."""
    release = threading.Event()

    def blocked_send(url, body, content_type):
        release.wait(5)
        return 204, {}, b""

    url_file = tmp_path / "url"
    url_file.write_text("https://discord.example/hook")
    relay = discord_relay.Relay(
        str(url_file), max_queue=10, deadline=900.0, send=blocked_send
    )
    worker = threading.Thread(target=relay.work, daemon=True)
    worker.start()
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), discord_relay.make_handler(relay, worker)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        statuses = []
        for _ in range(2):
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=2
            )
            conn.request(
                "POST", "/", body=b"{}", headers={"Content-Type": "application/json"}
            )
            statuses.append(conn.getresponse().status)
            conn.close()
        assert statuses == [204, 204]
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_the_script_holds_nothing_flux_would_substitute():
    """The script ships in a ConfigMap that Flux's postBuild substitution reads."""
    assert "$" not in Path(discord_relay.__file__).read_text()
