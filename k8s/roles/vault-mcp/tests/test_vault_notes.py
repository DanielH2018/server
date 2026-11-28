import os

import pytest
import yaml
from _k8s_render import render_role_template

from vault_notes import NoteNotFound, Vault

NOTES = {
    "Projects/Alpha.md": (
        "---\ntags: [project/alpha]\nstatus: active\n---\n"
        "Alpha brews tea in the kettle. See [[Beta]], [[Secret]] and [Gamma](../Reference/Gamma.md).\n"
        "#inbox\n"
    ),
    "Projects/Beta.md": "Beta mentions the kettle twice: kettle. Back to [[Alpha|the alpha note]].\n",
    "Reference/Gamma.md": "Gamma is reference material about teapots.\n",
    "94 Docs/Visible.md": "A visible kettle note beside the hidden folder. [[Alpha]]\n",
    "94 Docs/superpowers-x/Kept.md": "A kettle note in a sibling folder. [[Alpha]]\n",
}
HIDDEN = {
    ".obsidian/workspace.md": "kettle [[Alpha]]\n",
    "Projects/.trash/Old.md": "kettle [[Alpha]]\n",
    "94 Docs/superpowers/Secret.md": "kettle [[Alpha]]\n",
    "94 Docs/superpowers/deep/Deeper.md": "kettle [[Alpha]]\n",
}
EXCLUDE = ["94 Docs/superpowers/"]


@pytest.fixture
def vault(tmp_path):
    for i, (rel, text) in enumerate({**NOTES, **HIDDEN}.items()):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
    return Vault(tmp_path, EXCLUDE)


def test_search_finds_notes_holding_every_term_best_match_first(vault):
    hits = vault.search("kettle")
    assert [h["path"] for h in hits][:1] == ["Projects/Beta.md"]
    assert {h["path"] for h in hits} == {
        "Projects/Alpha.md",
        "Projects/Beta.md",
        "94 Docs/Visible.md",
        "94 Docs/superpowers-x/Kept.md",
    }
    assert [h["path"] for h in vault.search("kettle tea")] == ["Projects/Alpha.md"]
    assert "kettle" in hits[0]["snippet"]


def test_search_filters_by_tag_and_by_folder(vault):
    assert [h["path"] for h in vault.search(tag="project")] == ["Projects/Alpha.md"]
    assert [h["path"] for h in vault.search(tag="#inbox")] == ["Projects/Alpha.md"]
    assert [h["path"] for h in vault.search("kettle", path="Projects")] == [
        "Projects/Beta.md",
        "Projects/Alpha.md",
    ]


def test_search_returns_no_hidden_note(vault):
    paths = {h["path"] for h in vault.search("kettle", limit=100)}
    assert not paths & HIDDEN.keys()


def test_read_note_returns_body_frontmatter_tags_and_resolved_links(vault):
    note = vault.read_note("Projects/Alpha")
    assert note["frontmatter"] == {"tags": ["project/alpha"], "status": "active"}
    assert note["body"].startswith("Alpha brews tea")
    assert note["tags"] == ["inbox", "project/alpha"]
    assert note["links"] == [
        {"target": "Beta", "path": "Projects/Beta.md"},
        {"target": "Secret", "path": None},
        {"target": "../Reference/Gamma.md", "path": "Reference/Gamma.md"},
    ]


@pytest.mark.parametrize("path", [*HIDDEN, "../outside.md", "/etc/passwd"])
def test_read_note_refuses_a_hidden_or_escaping_path(vault, path):
    with pytest.raises(NoteNotFound):
        vault.read_note(path)


def test_read_note_refuses_a_symlink_into_a_hidden_folder(vault):
    link = vault.root / "Projects" / "Alias.md"
    link.symlink_to(vault.root / "94 Docs" / "superpowers" / "Secret.md")
    with pytest.raises(NoteNotFound):
        vault.read_note("Projects/Alias.md")


def test_list_folder_lists_visible_entries_folders_first(vault):
    assert vault.list_folder() == [
        {"name": "94 Docs", "path": "94 Docs", "type": "folder"},
        {"name": "Projects", "path": "Projects", "type": "folder"},
        {"name": "Reference", "path": "Reference", "type": "folder"},
    ]
    assert [e["path"] for e in vault.list_folder("94 Docs")] == [
        "94 Docs/superpowers-x",
        "94 Docs/Visible.md",
    ]


@pytest.mark.parametrize(
    "folder", [".obsidian", "Projects/.trash", "94 Docs/superpowers"]
)
def test_list_folder_refuses_a_hidden_folder(vault, folder):
    with pytest.raises(NoteNotFound):
        vault.list_folder(folder)


def test_recent_notes_lists_visible_notes_newest_first(vault):
    assert [n["path"] for n in vault.recent_notes(limit=100)] == list(reversed(NOTES))


def test_backlinks_names_only_visible_linking_notes(vault):
    assert [b["path"] for b in vault.backlinks("Projects/Alpha.md")] == [
        "94 Docs/Visible.md",
        "94 Docs/superpowers-x/Kept.md",
        "Projects/Beta.md",
    ]


@pytest.mark.parametrize(
    "entry", ["94 Docs/superpowers", "94 docs/SUPERPOWERS/", "./94 Docs//superpowers"]
)
def test_an_exclude_path_matches_by_segment_whatever_its_spelling(tmp_path, entry):
    (tmp_path / "94 Docs" / "superpowers").mkdir(parents=True)
    (tmp_path / "94 Docs" / "superpowers" / "Secret.md").write_text("kettle")
    vault = Vault(tmp_path, [entry])
    assert vault.search("kettle") == []


def test_the_rendered_exclude_list_hides_the_operators_folder(vault):
    pod = yaml.safe_load(render_role_template("vault-mcp", "deployment.yaml.j2"))[
        "spec"
    ]["template"]
    (server,) = [c for c in pod["spec"]["containers"] if c["name"] == "vault-mcp"]
    env = {e["name"]: e.get("value") for e in server["env"]}
    rendered = Vault(vault.root, env["VAULT_MCP_EXCLUDE_PATHS"].splitlines())
    assert {h["path"] for h in rendered.search("kettle", path="94 Docs")} == {
        "94 Docs/Visible.md",
        "94 Docs/superpowers-x/Kept.md",
    }
    with pytest.raises(NoteNotFound):
        rendered.read_note("94 Docs/superpowers/Secret.md")
