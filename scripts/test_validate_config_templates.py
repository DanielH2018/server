"""Tests for validate_config_templates — the non-compose config-template render guard."""
import validate_config_templates as v


def test_all_real_config_templates_render_to_valid_yaml():
    # The regression guard: every listed auth/proxy/monitoring config template must render with
    # stubbed vars to parseable YAML. Fails alongside the prek hook if an edit breaks indentation.
    ctx = {**v.BASE_CONTEXT, **v.load_yaml(v.ALL_VARS)}
    bad = {rel: v.check_template(rel, ctx) for rel in v.CONFIG_TEMPLATES}
    bad = {k: e for k, e in bad.items() if e is not None}
    assert not bad, f"config templates failed to render to valid YAML: {bad}"


def test_yaml_error_passes_valid_and_catches_invalid():
    assert v.yaml_error("a: 1\nb: 2\n") is None
    # Tab indentation / a bad mapping is rejected — proves the guard actually catches breakage.
    assert v.yaml_error("a:\n\t- broken: : :\n") is not None


def test_stub_undefined_survives_indent_filter():
    # The tricky case that a plain Undefined can't handle: `{{ secret | indent(n) }}`.
    env = v.build_env("authelia")
    assert env.from_string("{{ missing | indent(4) }}").render() == "STUB"


def test_config_templates_are_not_the_compose():
    # Guard against someone pointing this at a compose template (the other validator's job).
    assert all(not rel.endswith("docker-compose.yml.j2") for rel in v.CONFIG_TEMPLATES)
