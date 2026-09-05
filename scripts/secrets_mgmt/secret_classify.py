"""Which rotation tier a secret's NAME puts it in.

Pure name matching, no I/O and no imports outside the standard library: `secret_registry.sync()`
classifies each newly registered secret with this, and an operator overrides the verdict by
editing that secret's `tier` in `ansible/secret_rotation.yml` (`sync` preserves an override).

The tiers themselves, and the cadence each carries, are documented in
`scripts/secrets_mgmt/secret_rotation.py`'s module docstring and published as
`docs/reference/secrets.md`.
"""

# Classification by name. First matching rule wins; default is `assisted` (the safe,
# reminds-but-doesn't-touch tier).
_IGNORE = {"domain"}
_IGNORE_SUFFIX = ("_user", "_username")
_PINNED = {"authelia_storage", "zigbee_network_key"}
_EXTERNAL = {
    "cloudflare_dns_token",
    "monitor_discord_webhook_url",
    "crowdsec_discord_webhook_url",
    "gitops_deploy_discord_webhook",
    "coinmarket_api_key",
    "karakeep_gemini_api_key",
    "weather_api_key",
    "crowdsec_mapquest_api_key",
    "mullvad_account",
    "email",
    "healthchecks_smtp_password",
    "wireguard_interface_private_key",
}


def classify(name: str) -> str:
    """The rotation tier for a secret name: ignore, pinned, external, auto, or assisted."""
    if name in _IGNORE or name.endswith(_IGNORE_SUFFIX):
        return "ignore"
    if name in _PINNED:
        return "pinned"
    if name in _EXTERNAL:
        return "external"
    if name.endswith("_push_token"):
        return "auto"
    return "assisted"
