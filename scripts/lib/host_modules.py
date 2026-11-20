"""Puts the host-side module directories on `sys.path`: the one module under `scripts/` that may.

`hosts/lib` and the gh-quota component's `files/` ship to the hosts as flat files, so their
modules import by bare name: `host_lib`, `ci_verdict`, `builder_ci_grade`, `gh_quota`. A module
under `scripts/` that needs one imports this module first, for its effect:

    from lib import host_modules  # noqa: F401
    from ci_verdict import CIVerdict

A module a host package copies (a `shared:` entry) cannot import this, because no `lib` ships
beside it, so it keeps its own insert (docs/adr/0039-scripts-is-installed-in-place.md).

Typical usage example:

    from lib import host_modules  # noqa: F401
    import host_lib
"""

import sys

from lib.repo_paths import GH_QUOTA_FILES, HOST_LIB_FILES

sys.path.insert(0, str(GH_QUOTA_FILES))
sys.path.insert(0, str(HOST_LIB_FILES))
