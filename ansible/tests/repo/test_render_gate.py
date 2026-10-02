"""The render gate's detector, spelling by spelling: what counts as reading a template.

`_render_gate.py` resolves a template path through five spellings, and the gate it feeds —
`test_guard_tests_read_renders_not_templates.py` — is only as good as that resolution. Each
assertion below is one spelling a guard in this repo actually used; three of them were invisible
until #3209, #3210 and #3211.

The two negative tests matter as much: a hand-rolled render and a `tmp_path` fixture both read a
template and neither is the thing the gate forbids.

Run: uv run pytest ansible/tests/repo/test_render_gate.py
"""

from _render_gate import template_source_reads


# Every spelling of a source read a guard in this tree has used. Named at module level and
# counted below, because the loop over it passes vacuously if a case is ever dropped.
FLAGGED_SPELLINGS = (
    'T = K8S_ROLES / "n8n" / "templates" / "Dockerfile.j2"\nT.read_text()\n',
    'T = ANSIBLE / "roles/k8s/uptime-kuma/templates/x.yaml.j2"\nT.read_bytes()\n',
    '(K8S_ROLES / "authelia" / "templates" / "c.yaml.j2").read_text()\n',
    'for p in ROLES.glob("*/*/templates/**/*.j2"):\n    p.read_text()\n',
    'TEXT = [p.read_text() for p in ROOT.rglob("*.j2")]\n',
    'A = ROLES / "k8s/a/templates/x.j2"\nB = ROLES / "k8s/b/templates/y.j2"\n'
    "for t in (A, B):\n    t.read_text()\n",
    'open(ROLES / "k8s" / "a" / "templates" / "x.j2")\n',
    # The wrapped glob of #3209: `sorted()` sat where the detector looked for the `glob`.
    'for tpl in sorted(role_dir.glob("templates/*.j2")):\n    tpl.read_text()\n',
    # #3211's spelling: the pattern names no template, and the directory it globs does.
    'tdir = role / "templates"\nfor t in sorted(tdir.glob("*.j2")):\n    t.read_text()\n',
    # The same read with a pattern that names nothing at all, which the unwrap alone misses:
    # only the bound DIRECTORY says these are templates.
    'tdir = role / "templates"\nTEXT = {t.name: t.read_text() for t in tdir.glob("*")}\n',
    # The helper parameter of #3209: no template path at the read site.
    "def f(p):\n    return p.read_text()\n\n\n"
    'for tpl in K.glob("*/templates/*.j2"):\n    f(tpl)\n',
)


def test_a_template_source_read_is_flagged_in_each_spelling():
    assert len(FLAGGED_SPELLINGS) >= 11, "a spelling was dropped from the corpus"
    for source in FLAGGED_SPELLINGS:
        assert template_source_reads(source) != [], source


def test_a_template_path_imported_from_a_sibling_module_is_flagged():
    """#3210: the path is bound in one module and read in another.

    `staging/test_staging_egress_fence.py` read `_fence_probe.ORCHESTRATOR` that way, and the
    census named the module as reading three templates while missing the fourth. The sibling is
    passed in rather than read off disk, so this proof does not go quiet the day
    `_fence_probe.py` stops binding that name.
    """
    sibling = {
        "_fence_probe": 'HYPERVISOR = ROLES / "setup" / "hypervisor"\n'
        'ORCHESTRATOR = HYPERVISOR / "templates" / "etcd-restore-drill-vm.sh.j2"\n'
    }
    assert template_source_reads(
        "from _fence_probe import ORCHESTRATOR\nORCHESTRATOR.read_text()\n", sibling
    ) == ["ORCHESTRATOR.read_text()"]
    # A name the exporting module does not bind to a template carries nothing.
    assert (
        template_source_reads(
            "from _fence_probe import HOST_VARS\nHOST_VARS.read_text()\n",
            {"_fence_probe": 'HOST_VARS = ANSIBLE / "inventory" / "host_vars"\n'},
        )
        == []
    )
    # A module that is not importable from the test's directory does not resolve.
    assert (
        template_source_reads(
            "from _elsewhere import ORCHESTRATOR\nORCHESTRATOR.read_text()\n", {}
        )
        == []
    )


def test_a_hand_rolled_render_is_not_flagged():
    """A read whose result goes into Jinja is a render, not an assertion on template text.

    `setup/test_kuma_check_timer.py` renders its two units this way, because it needs a
    different override per test. The same read is a source read when its result is asserted on
    instead, which is the last entry of `FLAGGED_SPELLINGS`.
    """
    assert (
        template_source_reads(
            "def _render(template, **overrides):\n"
            "    return env.from_string(template.read_text()).render(overrides)\n\n\n"
            'SERVICE = COMMON / "templates" / "kuma-check.service.j2"\n'
            "_render(SERVICE)\n"
        )
        == []
    )


def test_a_render_and_a_fixture_are_not_flagged():
    assert (
        template_source_reads(
            "from _k8s_render import rendered_texts\n"
            'TEXT = rendered_texts("authelia")["config-secret.yaml.j2"]\nTEXT.count("x")\n'
        )
        == []
    )
    # A role's `files/` copy ships verbatim: there is no render, so the text is the artifact.
    assert (
        template_source_reads(
            'T = ANSIBLE / "roles/k8s/uptime-kuma/files/email-message.liquid"\n'
            "T.read_text()\n"
        )
        == []
    )
    # A synthetic template the module wrote itself.
    assert (
        template_source_reads(
            'tpl = tmp_path / "templates" / "x.j2"\ntpl.write_text("")\ntpl.read_text()\n'
        )
        == []
    )
    # An inventory read is not a template read.
    assert (
        template_source_reads(
            'HOST_VARS = ANSIBLE / "inventory" / "host_vars"\n'
            'for f in HOST_VARS.glob("*.yml"):\n    f.read_text()\n'
        )
        == []
    )
