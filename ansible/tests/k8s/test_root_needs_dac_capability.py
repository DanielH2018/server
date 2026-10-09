"""Tree-wide guard: root that drops ALL capabilities must add a DAC capability back.

WHY THIS EXISTS. In this cluster's pods, `runAsUser: 0` with `drop: [ALL]` is WEAKER than
running as the pod's own uid, not stronger. Root's ability to ignore file permission bits is
the DAC_OVERRIDE capability, so dropping ALL takes it away — and root then cannot read or write
files owned by another uid, which is the only reason anyone reaches for root in the first place.

Six containers in four templates combine `runAsUser: 0` with `drop: [ALL]`. Every one records
the lesson in a comment beside the fix, which is why this guard has real accept cases rather
than synthetic ones:

  * `loki-homelab/templates/alloy-daemonset.yaml.j2`, container `alloy`, adds DAC_READ_SEARCH —
    syslog/auth.log are owned by the `syslog` user at 640, so root without it cannot read them.
  * `code-server/templates/deployment.yaml.j2`, init container `seed-workspace-claim`, adds
    CHOWN + DAC_OVERRIDE + FOWNER — a fresh claim's root is root:root while the files being
    copied belong to the pod uid.
  * The other four are the CrowdSec seeding init containers, one pair rendered into each of
    `authelia/templates/deployment.yaml.j2` and `traefik/templates/deployment.yaml.j2` from the
    shared `ansible/templates/crowdsec-agent.yml.j2`. `crowdsec-hub-install` adds CHOWN + DAC_READ_SEARCH: it reads the image's root-only staged
    hub tree and hands the copy to the pod uid. `crowdsec-data-install` adds DAC_READ_SEARCH
    alone, to read the 0600 data sources into a root-owned emptyDir it can already write.

The third seeding container, `crowdsec-config-install`, runs as the pod's uid and so is not a
root site.

The hazard is the COMBINATION, never root by itself.

WHAT IT READS. Every role template in all three planes, as the deploy renders it: the k8s
manifests from `_k8s_render.rendered_texts`, each role's `Dockerfile*.j2` from
`rendered_build_texts`, the setup plane from `_setup_render.rendered_setup_texts`, the Pi's
Docker roles from `_compose_render.rendered_texts`, and every `*.sh.j2` in any plane from
`_shell_render.rendered_shell_texts`. No single render covers that file set, so `template_texts`
indexes all five by (plane, role, template name) and reads the SOURCE only for the three
templates none of them reaches — `FALLBACK_TEMPLATES`, held by
`test_the_render_reaches_every_role_template`. Reading the render is what lets the scan see a
securityContext a Jinja macro builds, or a `runAsUser` that arrives through a variable.

WHAT THE REAL-TREE ASSERTION IS WORTH HERE. Zero violations today, so the real-tree half passing
is not by itself evidence the rule works — a rule matching nothing would pass identically. The
synthetic reject cases below are that evidence. What the real tree does buy is genuine accept
coverage: six production securityContexts exercise the clean paths, both the DAC_OVERRIDE form
and the read-only DAC_READ_SEARCH form, and
`test_every_documented_root_site_still_renders_a_clean_root_block` names all six so a template
that stops rendering one fails here instead of quietly emptying the accept set. The render can
empty it two ways a source scan could not: a `{% if %}` around a root block that is false under
daniel-box's inventory, and a macro that emits `capabilities:` before `runAsUser:` — which
`ansible/templates/security-context.yml.j2` does not, so the forward scan below still reaches
the block's own `drop:`/`add:`.
"""

import re
from pathlib import Path

from _compose_render import rendered_texts as _rendered_compose_texts
from _helpers import ROLES as _ROLES
from _k8s_render import rendered_build_texts as _rendered_build_texts
from _k8s_render import rendered_texts as _rendered_k8s_texts
from _setup_render import rendered_setup_texts as _rendered_setup_texts
from _shell_render import rendered_shell_texts as _rendered_shell_texts

# The two capabilities that give root back its permission-bit override. DAC_OVERRIDE is the
# read+write form; DAC_READ_SEARCH is the read-only form, which is enough for a log tailer.
_DAC_CAPS = ("DAC_OVERRIDE", "DAC_READ_SEARCH")

_ROOT = re.compile(r"^\s*runAsUser:\s*0\s*$")
_DROP_ALL = re.compile(r"^\s*-\s*ALL\s*$")
_ADD = re.compile(r"^\s*add:\s*$")
_DROP = re.compile(r"^\s*drop:\s*$")


# The two role templates no render reaches: `_k8s_render` skips `k8s/image-builder`, an
# `include_role` helper with no `containers_list` entry, so its image and tag arrive on the
# calling role's include task. Their source is read
# instead, and `test_the_render_reaches_every_role_template` fails if the set grows.
FALLBACK_TEMPLATES = frozenset(
    {
        "k8s/image-builder/templates/build-job.yaml.j2",
        "k8s/image-builder/templates/context-configmap.yaml.j2",
    }
)


def _manifest_files() -> list[Path]:
    return sorted(p for p in _ROLES.rglob("templates/*.j2") if "archive" not in p.parts)


def _rendered_index() -> dict[tuple[str, str, str], str]:
    """Every plane's renders, keyed (plane, role, template name).

    The plane is part of the key because all five readers return a bare role name, and a role
    name that existed in two planes would otherwise hand one plane's guard the other's render.
    """
    index: dict[tuple[str, str, str], str] = {}
    for plane, texts in (
        ("k8s", _rendered_k8s_texts()),
        ("k8s", _rendered_build_texts()),
        ("setup", _rendered_setup_texts()),
        ("containers", _rendered_compose_texts()),
    ):
        for role, template, text in texts:
            index[(plane, role, template)] = text
    for plane, role, template, text in _rendered_shell_texts():
        index[(plane, role, template)] = text
    return index


def template_texts() -> dict[str, str]:
    """Every role template keyed `<plane>/<role>/templates/<name>`, rendered where a render reaches it.

    A template in `FALLBACK_TEMPLATES` contributes its source text, which is the only read left
    here — the reason this module stays in `TEMPLATE_SOURCE_READERS`.
    """
    index = _rendered_index()
    texts = {}
    for path in _manifest_files():
        plane = path.relative_to(_ROLES).parts[0]
        rendered = index.get((plane, path.parent.parent.name, path.name))
        key = str(path.relative_to(_ROLES))
        texts[key] = rendered if rendered is not None else path.read_text()
    return texts


def _root_blocks(text: str) -> list[tuple[int, bool, bool]]:
    """Each `runAsUser: 0` block in `text` as (line number, drops ALL, adds a DAC capability).

    Scans the securityContext following each `runAsUser: 0` rather than parsing YAML, because
    these are Jinja templates: a `{% if %}` around a block is legal here and would make a YAML
    parse fail on a file this guard still needs to read.
    """
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        if not _ROOT.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        drops_all = adds_dac = False
        section = None
        for later in lines[i + 1 :]:
            if later.strip() and (len(later) - len(later.lstrip())) < indent:
                break  # left this securityContext
            if _DROP.match(later):
                section = "drop"
            elif _ADD.match(later):
                section = "add"
            elif section == "drop" and _DROP_ALL.match(later):
                drops_all = True
            elif section == "add" and any(cap in later for cap in _DAC_CAPS):
                adds_dac = True
        blocks.append((i + 1, drops_all, adds_dac))
    return blocks


def root_without_dac(text: str) -> list[int]:
    """Line numbers of `runAsUser: 0` blocks that drop ALL and add no DAC capability."""
    return [
        line_no
        for line_no, drops_all, adds_dac in _root_blocks(text)
        if drops_all and not adds_dac
    ]


def _dac_root_blocks(text: str) -> int:
    """How many `runAsUser: 0` blocks in `text` drop ALL and add a DAC capability back.

    The accept case is that COMBINATION, so a count of unflagged root blocks would not do: a
    block that lost its `drop: [ALL]` is also unflagged, and the accept coverage would read as
    intact while no longer exercising the pair of fields this guard is about.
    """
    return sum(
        1 for _, drops_all, adds_dac in _root_blocks(text) if drops_all and adds_dac
    )


def test_no_manifest_runs_root_with_all_capabilities_dropped() -> None:
    """The real tree. See the module docstring on what this passing does and does not prove."""
    offenders = []
    for key, text in sorted(template_texts().items()):
        for line_no in root_without_dac(text):
            offenders.append(f"ansible/roles/{key} (rendered line {line_no})")

    assert not offenders, (
        "these containers run as root with `drop: [ALL]` and add no DAC capability, which "
        "makes root WEAKER than the pod's own uid — it cannot read or write another uid's "
        "files. Add DAC_OVERRIDE (read+write) or DAC_READ_SEARCH (read-only), or do not run "
        "as root:\n  " + "\n  ".join(offenders)
    )


def test_the_render_reaches_every_role_template() -> None:
    """The source-read half of `template_texts`: only `FALLBACK_TEMPLATES` falls back.

    A template dropping out of its plane's render would read as source again and stop showing
    a macro-built securityContext, which is the whole point of reading the render.
    """
    index = _rendered_index()
    fell_back = {
        str(path.relative_to(_ROLES))
        for path in _manifest_files()
        if (
            path.relative_to(_ROLES).parts[0],
            path.parent.parent.name,
            path.name,
        )
        not in index
    }
    assert fell_back == set(FALLBACK_TEMPLATES), (
        "the set of templates no render reaches has moved. Templates now falling back to "
        f"source: {sorted(fell_back - FALLBACK_TEMPLATES)}; pinned entries now rendered: "
        f"{sorted(FALLBACK_TEMPLATES - fell_back)}"
    )


def test_every_documented_root_site_still_renders_a_clean_root_block() -> None:
    """Non-vacuity: the six accept cases the module docstring names are still in the corpus.

    Counted off the render, so a `{% if %}` that stopped emitting a root block, or a block
    moved into a macro that emits its capabilities first, arrives as a missing accept case
    rather than as a guard quietly covering nothing.
    """
    expected = {
        "k8s/loki-homelab/templates/alloy-daemonset.yaml.j2": 1,
        "k8s/code-server/templates/deployment.yaml.j2": 1,
        "k8s/authelia/templates/deployment.yaml.j2": 2,
        "k8s/traefik/templates/deployment.yaml.j2": 2,
    }
    texts = template_texts()
    found = {key: _dac_root_blocks(texts.get(key, "")) for key in expected}
    assert found == expected, (
        "these templates are this guard's only accept coverage — a `runAsUser: 0` block that "
        f"drops ALL and adds a DAC capability back. Expected {expected}, rendered {found}"
    )


def test_root_dropping_all_with_no_dac_is_flagged() -> None:
    doc = """
        securityContext:
          runAsUser: 0
          capabilities:
            drop:
              - ALL
    """
    assert root_without_dac(doc) == [3]


def test_root_dropping_all_that_adds_dac_override_is_clean() -> None:
    """code-server's live shape: it needs to read the pod uid's files and hand them back."""
    doc = """
        securityContext:
          runAsUser: 0
          capabilities:
            drop:
              - ALL
            add:
              - CHOWN
              - DAC_OVERRIDE
              - FOWNER
    """
    assert root_without_dac(doc) == []


def test_root_dropping_all_that_adds_only_read_search_is_clean() -> None:
    """The Alloy shipper's live shape: read-only is enough for a log tailer, so the narrower cap counts."""
    doc = """
        securityContext:
          runAsUser: 0
          capabilities:
            drop:
              - ALL
            add:
              - DAC_READ_SEARCH
    """
    assert root_without_dac(doc) == []


def test_root_that_drops_nothing_is_clean() -> None:
    """Root that drops nothing keeps DAC_OVERRIDE. The hazard is the COMBINATION."""
    doc = """
        securityContext:
          runAsUser: 0
    """
    assert root_without_dac(doc) == []


def test_a_non_root_container_dropping_all_is_clean() -> None:
    """Dropping ALL is the correct default for an ordinary uid and must not be flagged."""
    doc = """
        securityContext:
          runAsUser: 1000
          capabilities:
            drop:
              - ALL
    """
    assert root_without_dac(doc) == []


def test_a_dac_cap_added_to_a_LATER_container_does_not_excuse_this_one() -> None:
    """The scan must stop at the end of this securityContext, or one fix would clear them all."""
    doc = """
        containers:
          - name: bad
            securityContext:
              runAsUser: 0
              capabilities:
                drop:
                  - ALL
          - name: good
            securityContext:
              runAsUser: 0
              capabilities:
                drop:
                  - ALL
                add:
                  - DAC_OVERRIDE
    """
    assert root_without_dac(doc) == [5]
