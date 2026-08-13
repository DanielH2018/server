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


def test_skips_config_mandatory_images():
    # authelia + couchdb + tempo crash on a bare `docker run` (no config/creds), so image-smoke
    # must not try to boot them or the required check false-fails; any tag is skipped.
    diff = (
        "+    image: authelia/authelia:4.39.20\n"
        "+    image: couchdb:3.5.2\n"
        "+    image: grafana/tempo:2.10.7\n"
    )
    assert extract_changed_images(diff) == []


def test_skips_never_healthy_image():
    # karakeep boots and stays up but its baked healthcheck never reaches "healthy" within
    # image-smoke's poll window, so image-smoke false-fails on its Renovate digest bumps; skip
    # it by repository like the hard-exit ones (its real config is covered by the host health gate).
    diff = (
        "+    image: ghcr.io/karakeep-app/karakeep:release"
        "@sha256:64d6a9bbf2d37b5c808cf06b5d87f1f1c7846fdd3844724145a9741aeb06fd31\n"
    )
    assert extract_changed_images(diff) == []


def test_skip_list_is_repo_scoped_not_substring():
    # A different repo whose name merely contains a skipped one is still smoked.
    diff = "+    image: ghcr.io/example/couchdb-exporter:1.0\n"
    assert extract_changed_images(diff) == ["ghcr.io/example/couchdb-exporter:1.0"]


def test_skips_digest_pinned_skiplist_image():
    diff = "+    image: couchdb:3.5.2@sha256:abc123\n"
    assert extract_changed_images(diff) == []


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


def test_k8s_default_image_skiplist_repo_still_skipped():
    diff = (
        "+karakeep_k8s_image: ghcr.io/karakeep-app/karakeep:release"
        "@sha256:64d6a9bbf2d37b5c808cf06b5d87f1f1c7846fdd3844724145a9741aeb06fd31\n"
    )
    assert extract_changed_images(diff) == []


def test_k8s_default_image_ignores_removed_and_non_image_vars():
    diff = (
        "-cloudflare_ddns_k8s_image: favonia/cloudflare-ddns:latest\n"
        '+cloudflare_ddns_k8s_cpu_limit: "500m"\n'
    )
    assert extract_changed_images(diff) == []
