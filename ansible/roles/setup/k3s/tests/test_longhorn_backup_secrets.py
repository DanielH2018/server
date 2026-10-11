"""The R2 and B2 credential Secrets render `data:`, so a re-apply reads `unchanged` (#4341).

Client-side `kubectl apply` reports `configured` for a `stringData` Secret on every run, which
left both apply tasks in `tasks/longhorn-backup.yml` reporting changed whatever the inputs.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_backup_secrets.py
"""

import base64

import pytest

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import K3S_ROLE

CASES = {
    "longhorn-r2-secret.yaml.j2": (
        {
            "r2_access_key_id": "r2-id",
            "r2_secret_access_key": "r2/se+cret=",
            "r2_account_id": "acct",
        },
        {
            "AWS_ACCESS_KEY_ID": "r2-id",
            "AWS_SECRET_ACCESS_KEY": "r2/se+cret=",
            "AWS_ENDPOINTS": "https://acct.r2.cloudflarestorage.com",
        },
    ),
    "longhorn-b2-secret.yaml.j2": (
        {
            "longhorn_b2_key_id": "b2-id",
            "longhorn_b2_application_key": "b2/se+cret=",
            "k3s_longhorn_b2_region": "us-west-004",
        },
        {
            "AWS_ACCESS_KEY_ID": "b2-id",
            "AWS_SECRET_ACCESS_KEY": "b2/se+cret=",
            "AWS_ENDPOINTS": "https://s3.us-west-004.backblazeb2.com",
        },
    ),
}


@pytest.mark.parametrize("template", sorted(CASES))
def test_the_secret_renders_data_that_decodes_to_its_inputs(template):
    inputs, expected = CASES[template]
    env = make_ansible_env([K3S_ROLE / "templates"])
    doc = yaml_fast.safe_load(env.get_template(template).render(**inputs))
    assert "stringData" not in doc
    decoded = {k: base64.b64decode(v).decode() for k, v in doc["data"].items()}
    assert decoded == expected
