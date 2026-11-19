import pytest
from _gh_quota_fakes import SESSION, make_ledger


@pytest.fixture
def recording(tmp_path):
    """A ledger in `record` mode, with the pieces a test moves: (ledger, clock, gh)."""
    return make_ledger(tmp_path, "record", {"CLAUDE_CODE_SESSION_ID": SESSION})
