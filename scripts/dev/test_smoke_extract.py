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


def test_config_required_images_are_still_emitted():
    # These four are the shapes that used to be filtered out by _SKIP_BARE_BOOT: two that
    # hard-exit without their config, one whose baked healthcheck never reaches "healthy" in
    # the poll window, and one whose Cmd is a CLI binary that prints help and exits 1.
    #
    # Emitting them is the point. The workflow's fatal checks do not execute the image, so a
    # config-required entrypoint no longer decides anything; excluding them had also excluded
    # them from the `docker pull`, leaving those refs verified by nothing at all.
    diff = (
        "+    image: authelia/authelia:4.39.20\n"
        "+    image: couchdb:3.5.2@sha256:abc123\n"
        "+    image: ghcr.io/karakeep-app/karakeep:release\n"
        "+karakeep_k8s_tagger_image: ghcr.io/astral-sh/uv:python3.14-bookworm-slim\n"
    )
    assert extract_changed_images(diff) == [
        "authelia/authelia:4.39.20",
        "couchdb:3.5.2@sha256:abc123",
        "ghcr.io/karakeep-app/karakeep:release",
        "ghcr.io/astral-sh/uv:python3.14-bookworm-slim",
    ]


def test_extracts_k8s_default_image_var():
    diff = (
        "-homepage_k8s_image: ghcr.io/gethomepage/homepage:latest\n"
        "+homepage_k8s_image: ghcr.io/gethomepage/homepage:latest"
        "@sha256:61013368c8f95981c0bb8bf56d962078d8b4e95724a554fa2dabb20d6e478097\n"
    )
    assert extract_changed_images(diff) == [
        "ghcr.io/gethomepage/homepage:latest"
        "@sha256:61013368c8f95981c0bb8bf56d962078d8b4e95724a554fa2dabb20d6e478097"
    ]


def test_k8s_default_image_var_strips_quotes_and_trailing_comment():
    diff = '+freshrss_k8s_cache_image: "nginx:alpine"  # cache sidecar\n'
    assert extract_changed_images(diff) == ["nginx:alpine"]


def test_k8s_default_jinja_templated_image_is_skipped():
    # Registry-built images (n8n, ical-proxy, homelab-mcp, code-server) have no stable upstream
    # ref for image-smoke to pull — their FROM line is what Renovate tracks (see
    # REGISTRY_BUILT_IMAGES in test_renovate_managers.py). The Jinja value has a space inside
    # its quotes, so the k8s-default regex can't reach a closing quote/comment and never matches.
    diff = '+n8n_k8s_image: "{{ k8s_registry_pull_host }}/n8n:latest"\n'
    assert extract_changed_images(diff) == []


def test_k8s_default_image_var_emits_a_digest_pinned_ref():
    diff = (
        "+karakeep_k8s_image: ghcr.io/karakeep-app/karakeep:release"
        "@sha256:64d6a9bbf2d37b5c808cf06b5d87f1f1c7846fdd3844724145a9741aeb06fd31\n"
    )
    assert extract_changed_images(diff) == [
        "ghcr.io/karakeep-app/karakeep:release"
        "@sha256:64d6a9bbf2d37b5c808cf06b5d87f1f1c7846fdd3844724145a9741aeb06fd31"
    ]


def test_k8s_default_image_ignores_removed_and_non_image_vars():
    diff = (
        "-cloudflare_ddns_k8s_image: favonia/cloudflare-ddns:latest\n"
        '+cloudflare_ddns_k8s_cpu_limit: "500m"\n'
    )
    assert extract_changed_images(diff) == []
