"""Unit tests for smoke_extract.extract_changed_images (image-diff parser).

Run: uv run pytest scripts
"""

from smoke_extract import extract_changed_images


def test_extracts_added_image_line():
    diff = (
        "diff --git a/ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2 "
        "b/ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2\n"
        "--- a/...\n+++ b/...\n"
        "@@ -1 +1 @@\n"
        "-    image: ghcr.io/google/cadvisor:v0.53.0\n"
        "+    image: ghcr.io/google/cadvisor:v0.54.0\n"
    )
    assert extract_changed_images(diff) == ["ghcr.io/google/cadvisor:v0.54.0"]


def test_ignores_removed_and_context_lines():
    diff = (
        "-    image: foo:1\n"
        "     image: bar:2\n"  # context line (leading space), not added
        "+    image: foo:2\n"
    )
    assert extract_changed_images(diff) == ["foo:2"]


def test_strips_quotes():
    diff = '+    image: "louislam/uptime-kuma:2"\n'
    assert extract_changed_images(diff) == ["louislam/uptime-kuma:2"]


def test_ignores_non_image_additions():
    diff = "+    container_name: cadvisor\n+    restart: unless-stopped\n"
    assert extract_changed_images(diff) == []


def test_dedupes():
    diff = "+    image: foo:2\n+    image: foo:2\n"
    assert extract_changed_images(diff) == ["foo:2"]
