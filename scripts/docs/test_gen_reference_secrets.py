"""Tests for scripts/docs/gen_reference_secrets.py.

The page this generates is committed and served behind SSO, so the assertion that
matters is about what the generator READS, not what it prints.

Run: uv run pytest scripts/test_gen_reference_secrets.py
"""

from __future__ import annotations

import builtins
import datetime as dt
import textwrap

import gen_reference_secrets as g

TODAY = dt.date(2026, 8, 24)


def _registry(tmp_path):
    path = tmp_path / "secret_rotation.yml"
    path.write_text(
        textwrap.dedent("""\
        secrets:
          arr_autoblock_push_token:
            last_rotated: '2026-08-10'
            tier: auto
          authelia_storage_key:
            last_rotated: '2025-10-08'
            tier: pinned
          some_external_thing:
            last_rotated: '2026-01-02'
            tier: external
        """)
    )
    return path


def test_reads_only_the_plaintext_registry(tmp_path, monkeypatch):
    """The whole safety property, asserted behaviourally.

    A generator that can reach ansible/vars/secrets.yml -- or shell out to the
    decryption tool -- is one bug away from committing plaintext credentials into a
    browsable page. Recording the paths it opens proves it cannot, where a scan of the
    source text would only prove which words it contains.
    """
    registry = _registry(tmp_path)
    opened: list[str] = []

    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    g.build_rows(registry, TODAY)

    assert opened, "recorded nothing; the patch missed the read path"
    for path in opened:
        assert "vars/secrets" not in path, f"read the encrypted store: {path}"
    assert all(str(registry) == p or "secret_rotation" in p for p in opened), opened


def test_never_spawns_a_subprocess(tmp_path, monkeypatch):
    """Decryption would have to shell out; nothing here may."""
    registry = _registry(tmp_path)

    def explode(*args, **kwargs):
        raise AssertionError(f"generator spawned a subprocess: {args}")

    monkeypatch.setattr("subprocess.run", explode)
    monkeypatch.setattr("subprocess.Popen", explode)
    g.build_rows(registry, TODAY)


def test_rows_carry_name_tier_and_dates(tmp_path):
    rows = {r["name"]: r for r in g.build_rows(_registry(tmp_path), TODAY)}
    assert rows["arr_autoblock_push_token"]["tier"] == "auto"
    assert rows["arr_autoblock_push_token"]["last_rotated"] == "2026-08-10"


def test_due_dates_come_from_secret_rotation(tmp_path):
    """Not a second implementation: two would drift, and the page would then disagree
    with the audit cron that actually pages."""
    import secret_rotation

    rows = {r["name"]: r for r in g.build_rows(_registry(tmp_path), TODAY)}
    expected = secret_rotation.due_date({"last_rotated": "2026-08-10", "tier": "auto"})
    assert rows["arr_autoblock_push_token"]["due"] == expected.isoformat()


def test_pinned_tier_is_rendered_first(tmp_path):
    """pinned is the tier where following the generic procedure causes damage."""
    out = g.render_markdown(g.build_rows(_registry(tmp_path), TODAY))
    assert out.index("## pinned") < out.index("## auto")
    assert "DANGER" in out


def test_no_secret_value_appears_in_the_page(tmp_path):
    """Belt and braces: the registry holds no values, so none can reach the page."""
    out = g.render_markdown(g.build_rows(_registry(tmp_path), TODAY))
    assert "ENC[" not in out
    assert "BEGIN" not in out


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    out = g.render_markdown(g.build_rows(_registry(tmp_path), TODAY))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/gen_reference_secrets.py" in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    out = g.render_markdown(g.build_rows(_registry(tmp_path), TODAY))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
