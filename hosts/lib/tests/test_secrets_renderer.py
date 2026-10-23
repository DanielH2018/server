"""The host renderer's secret-bearing files: declared outputs, compose files and `secrets.json`.

Each case is one of H4's *Verification* cases for H4.1a3. A render reads a fixture `hosts/` tree
under `tmp_path` against the repo's own inventory, so no case touches a real key or ciphertext.
The round trip runs the renderer's output through `hosts/lib/homelab-secrets` itself.

Run: uv run pytest hosts/lib/tests/test_secrets_renderer.py
"""

import json
import types

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import RenderError
from secrets_mgmt.host_secrets import tree_keys

SCRIPT = REPO / "hosts" / "lib" / "homelab-secrets"
REAL_COMPONENT = REPO / "hosts" / "components" / "secrets-unit"
TEMPLATES = "/usr/lib/homelab/secret-templates"
UNITS = "/usr/lib/systemd/system"
NAMES = ("alpha_token", "beta_token", "domain")
CIPHERTEXT = "sops:\n  version: fixture\nalpha_token: ENC[AES256_GCM,data:fixture]\n"


class Tree:
    """A fixture `hosts/` tree, a secrets file and a per-host ciphertext directory."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "hosts"
        self.secrets = tmp_path / "secrets.yml"
        self.sops_dir = tmp_path / "host-sops"
        self.secrets.write_text(yaml.safe_dump({name: "ENC[x]" for name in NAMES}))
        self.sops_dir.mkdir()
        (self.root / "lib").mkdir(parents=True)
        (self.root / "lib" / "homelab-secrets").write_text(SCRIPT.read_text())

    def package(
        self, host="daniel-server", components=("secrets-unit", "beat"), **extra
    ):
        spec = {"components": list(components), **extra}
        (self.root / host).mkdir(exist_ok=True)
        (self.root / host / "package.yml").write_text(yaml.safe_dump(spec))
        (self.sops_dir / f"{host}.sops.yaml").write_text(CIPHERTEXT)
        return self

    def component(self, name="beat", sources=None, **spec):
        directory = self.root / "components" / name
        directory.mkdir(parents=True, exist_ok=True)
        for src, text in (sources or {}).items():
            (directory / src).parent.mkdir(parents=True, exist_ok=True)
            (directory / src).write_text(text)
        (directory / "component.yml").write_text(yaml.safe_dump(spec))
        return self

    def render(self, host="daniel-server"):
        return host_mode.render_host(
            host, self.root, secrets=self.secrets, sops_dir=self.sops_dir
        )


@pytest.fixture
def tree(tmp_path):
    return (
        Tree(tmp_path)
        .component(
            "secrets-unit",
            files=[{"lib": "homelab-secrets", "dst": "/usr/lib/homelab/s"}],
        )
        .package()
    )


def output(**fields):
    fields = {
        "src": "o.j2",
        "path": "/etc/app/app.conf",
        "owner": "root",
        "group": "nut",
        "mode": "0640",
        "keys": ["alpha_token"],
        **fields,
    }
    return {"outputs": [fields]}


def text(package, dst):
    return package.files[dst][0].decode()


def secrets_json(package):
    return json.loads(text(package, "/usr/lib/homelab/secrets.json"))


def test_a_declared_output_renders_its_secret_as_a_placeholder_and_is_routed(tree):
    # `$` before `$`, before `{` and before a placeholder is a literal run C1 escapes.
    source = "pw=${{ alpha_token }} cost=$$5 sh=${HOME} plain={{ alpha_token }}\n"
    tree.component(sources={"o.j2": source}, **output())
    package = tree.render()
    assert text(package, f"{TEMPLATES}/etc/app/app.conf") == (
        "pw=$$${alpha_token} cost=$$$5 sh=$${HOME} plain=${alpha_token}\n"
    )
    assert package.links["/etc/app/app.conf"] == "/run/homelab-secrets/etc/app/app.conf"
    assert "/etc/app/app.conf" not in package.files


def test_an_output_without_a_link_ships_no_symlink_and_a_persisted_one_names_its_root(
    tree,
):
    tree.component(
        sources={"o.j2": "{{ alpha_token }}\n"}, **output(link=False, persist=True)
    )
    package = tree.render()
    assert package.links == {}
    (row,) = secrets_json(package)["outputs"]
    assert (row["persist"], row["mode"], row["destination"]) == (
        True,
        "0640",
        "etc/app/app.conf",
    )


def test_a_compose_file_keeps_its_placeholders_at_its_real_path_while_an_output_is_routed(
    tree,
):
    compose = "image: x\nenvironment:\n  PW: {{ beta_token }}\n  LIT: $$HOME\n"
    tree.component(
        sources={"o.j2": "{{ alpha_token }}\n", "c.yml.j2": compose},
        files=[
            {
                "src": "c.yml.j2",
                "dst": "/usr/lib/homelab/compose/c.yml",
                "compose": True,
            }
        ],
        **output(),
    )
    package = tree.render()
    assert text(package, "/usr/lib/homelab/compose/c.yml") == (
        "image: x\nenvironment:\n  PW: ${beta_token}\n  LIT: $$$HOME\n"
    )
    assert f"{TEMPLATES}/usr/lib/homelab/compose/c.yml" not in package.files
    assert f"{TEMPLATES}/etc/app/app.conf" in package.files
    assert [row["destination"] for row in secrets_json(package)["outputs"]] == [
        "etc/app/app.conf"
    ]


def test_a_compose_file_outside_the_compose_directory_is_flagged(tree):
    tree.component(
        sources={"c.j2": "x\n"},
        files=[{"src": "c.j2", "dst": "/etc/c.yml", "compose": True}],
    )
    with pytest.raises(RenderRefused, match="outside /usr/lib/homelab/compose"):
        tree.render()


@pytest.mark.parametrize("name", ["alpha_token", "domain"])
def test_an_undeclared_template_reading_a_secret_fails_and_names_the_key(tree, name):
    # `domain` is the case a stub would hide: the repo's other renderers stub it as example.com.
    tree.component(
        sources={"p.j2": "{{ %s }}\n" % name},
        files=[{"src": "p.j2", "dst": "/usr/lib/homelab/p"}],
    )
    with pytest.raises(RenderError, match=f"{name} is a secret key"):
        tree.render()


def test_an_output_reading_a_secret_it_does_not_declare_fails_and_names_the_key(tree):
    tree.component(sources={"o.j2": "{{ alpha_token }}{{ beta_token }}\n"}, **output())
    with pytest.raises(RenderError, match="beta_token is a secret key"):
        tree.render()


@pytest.mark.parametrize(
    ("template", "declared", "message"),
    [
        (
            "{{ alpha_token }}",
            ["alpha_token", "beta_token"],
            r"declared and unread \['beta_token'\]",
        ),
        (
            "{{ alpha_token | upper }}",
            ["alpha_token"],
            r"declared and unread \['alpha_token'\]",
        ),
        ("x", ["alpha_token"], r"declared and unread \['alpha_token'\]"),
        ("{{ alpha_token }}", ["no_such_key"], "not keys of secrets.yml"),
    ],
)
def test_declared_keys_must_match_what_the_template_reads(
    tree, template, declared, message
):
    tree.component(sources={"o.j2": template + "\n"}, **output(keys=declared))
    with pytest.raises(RenderRefused, match=message):
        tree.render()


def test_the_host_context_carries_inventory_hostname(tree):
    source = "{% if inventory_hostname == 'daniel-server' %}127.0.0.1{% endif %}\n"
    tree.component(
        sources={"p.j2": source}, files=[{"src": "p.j2", "dst": "/usr/lib/homelab/p"}]
    )
    assert text(tree.render(), "/usr/lib/homelab/p") == "127.0.0.1\n"
    tree.package("daniel-box")
    assert text(tree.render("daniel-box"), "/usr/lib/homelab/p") == "\n"


def _windowed(tree, mark):
    units = {"k3s.service": {"policy": "windowed"}} if mark else {}
    tree.component(
        sources={"o.j2": "{{ alpha_token }}\n", "k3s.service": "[Service]\n"},
        files=[{"src": "k3s.service", "dst": f"{UNITS}/k3s.service"}],
        units=units,
        **output(dependents=["k3s.service"]),
    )
    return tree


def test_an_output_whose_dependent_is_windowed_fails_and_names_the_unit(tree):
    with pytest.raises(
        RenderRefused, match=r"lists k3s\.service as a dependent.*windowed"
    ):
        _windowed(tree, True).render()


def test_the_same_package_without_the_windowed_mark_renders(tree):
    package = _windowed(tree, False).render()
    assert secrets_json(package)["outputs"][0]["dependents"] == ["k3s.service"]


@pytest.mark.parametrize("spec", [output(), {"credentials": ["alpha_token"]}])
def test_an_output_or_credential_without_the_secrets_unit_component_fails(
    tmp_path, spec
):
    tree = Tree(tmp_path).package(components=["beat"])
    tree.component(sources={"o.j2": "{{ alpha_token }}\n"}, **spec)
    with pytest.raises(RenderRefused, match="without the `secrets-unit` component"):
        tree.render()


def test_a_package_without_secrets_carries_no_secrets_field_or_file(tmp_path):
    tree = Tree(tmp_path).package(components=["beat"]).component()
    package = tree.render()
    assert "secrets_inputs_sha256" not in json.loads(
        text(package, "/usr/lib/homelab/manifest.json")
    )
    assert "/usr/lib/homelab/secrets.json" not in package.files


def test_the_secrets_json_names_the_ciphertext_credentials_and_outputs(tree):
    tree.component(
        sources={"o.j2": "{{ alpha_token }}\n"},
        credentials=["beta_token"],
        **output(dependents=["b.service", "a.service"], owner="root", group="root"),
    )
    package = tree.render()
    assert secrets_json(package) == {
        "sops_file": "/usr/lib/homelab/secrets/daniel-server.sops.yaml",
        "credentials": ["beta_token"],
        "outputs": [
            {
                "template": f"{TEMPLATES}/etc/app/app.conf",
                "destination": "etc/app/app.conf",
                "owner": "root",
                "group": "root",
                "mode": "0640",
                "persist": False,
                "dependents": ["a.service", "b.service"],
                "keys": ["alpha_token"],
            }
        ],
    }
    assert (
        text(package, "/usr/lib/homelab/secrets/daniel-server.sops.yaml") == CIPHERTEXT
    )


def test_the_inputs_hash_changes_with_the_ciphertext_a_template_and_the_manifest(tree):
    tree.component(sources={"o.j2": "a {{ alpha_token }}\n"}, **output())

    def manifest():
        rendered = text(tree.render(), "/usr/lib/homelab/manifest.json")
        return json.loads(rendered)["secrets_inputs_sha256"]

    first = manifest()
    assert manifest() == first
    (tree.sops_dir / "daniel-server.sops.yaml").write_text(CIPHERTEXT + "x: 1\n")
    second = manifest()
    tree.component(sources={"o.j2": "b {{ alpha_token }}\n"}, **output())
    third = manifest()
    tree.component(sources={"o.j2": "b {{ alpha_token }}\n"}, **output(mode="0600"))
    assert len({first, second, third, manifest()}) == 4


def test_a_package_that_ships_secrets_needs_its_ciphertext(tree):
    tree.component()
    (tree.sops_dir / "daniel-server.sops.yaml").unlink()
    with pytest.raises(RenderRefused, match=r"no daniel-server\.sops\.yaml to carry"):
        tree.render()


def test_a_shared_package_may_list_the_component_but_no_output(tree):
    spec = tree.root / "packages" / "shared" / "package.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text(yaml.safe_dump({"components": ["secrets-unit"]}))
    package = host_mode.render_package("shared", tree.root, secrets=tree.secrets)
    assert "/usr/lib/homelab/secrets.json" not in package.files
    tree.component(sources={"o.j2": "{{ alpha_token }}\n"}, **output())
    spec.write_text(yaml.safe_dump({"components": ["secrets-unit", "beat"]}))
    with pytest.raises(RenderRefused, match="a shared package lists no output"):
        host_mode.render_package("shared", tree.root, secrets=tree.secrets)


def test_the_renderers_key_names_are_the_ones_host_secrets_reads(tree):
    tree.component(
        sources={"o.j2": "{{ alpha_token }}\n"}, credentials=["beta_token"], **output()
    )
    package = tree.render()
    rendered = {*package.credentials, *(k for o in package.outputs for k in o["keys"])}
    assert rendered == {"alpha_token", "beta_token"}
    assert tree_keys(tree.root.parent, "daniel-server") == rendered


def _script(root, monkeypatch):
    monkeypatch.setenv("HOMELAB_ROOT", str(root))
    module = types.ModuleType("homelab_secrets")
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), module.__dict__)
    return module


def test_the_rendered_secrets_json_is_a_manifest_the_script_accepts(
    tree, tmp_path, monkeypatch
):
    tree.component(
        sources={"o.j2": "{{ alpha_token }}\n"}, credentials=["beta_token"], **output()
    )
    package = tree.render()
    root = tmp_path / "host"
    (root / "usr/lib/homelab").mkdir(parents=True)
    (root / "usr/lib/homelab/secrets.json").write_bytes(
        package.files["/usr/lib/homelab/secrets.json"][0]
    )
    assert _script(root, monkeypatch).load_manifest()["credentials"] == ["beta_token"]


def test_the_scripts_single_pass_restores_the_literal_text_the_renderer_escaped(
    tree, tmp_path, monkeypatch
):
    # `$$`, a shell `${VAR}`, and a `$` directly before a placeholder all survive the pair.
    literal = "a=$$ b=${VAR} c=$ d=$${x} e=$"
    tree.component(
        sources={"o.j2": literal + "${{ alpha_token }} f={{ alpha_token }}\n"},
        **output(),
    )
    rendered = text(tree.render(), f"{TEMPLATES}/etc/app/app.conf")
    module = _script(tmp_path, monkeypatch)
    got = module.substitute(rendered, {"alpha_token"}, {"alpha_token": "VALUE"})
    assert got == literal + "$VALUE f=VALUE\n"
    # A script that replaces `$${` first and `${key}` second reads the escaped run twice.
    naive = (
        rendered.replace("$${", "\0")
        .replace("${alpha_token}", "VALUE")
        .replace("\0", "${")
    )
    assert naive != got


def test_the_real_secrets_unit_component_renders_the_unit_and_the_script(tmp_path):
    tree = Tree(tmp_path).package(components=["secrets-unit"])
    real = REAL_COMPONENT
    target = tree.root / "components" / "secrets-unit"
    (target / "units").mkdir(parents=True)
    (target / "component.yml").write_text((real / "component.yml").read_text())
    unit = real / "units" / "homelab-secrets.service"
    (target / "units" / unit.name).write_text(unit.read_text())
    package = tree.render()
    service = text(package, f"{UNITS}/homelab-secrets.service")
    assert "Type=oneshot" in service and "RemainAfterExit=yes" in service
    assert "TimeoutStartSec=60" in service
    assert "SOPS_AGE_KEY_FILE=/etc/homelab/age.key" in service
    assert "ExecStart=/usr/lib/homelab/homelab-secrets" in service
    assert package.files["/usr/lib/homelab/homelab-secrets"][1] == 0o755
    assert package.depends.count("homelab-tools") == 1
