"""The git-tree lock's path for Python readers (ADR-0011, ADR-0017).

Every job that mutates or snapshots the primary checkout takes this lock. Host templates read
`server_git_tree_lock` from `group_vars/all.yml` instead, and
`scripts/lib/tests/test_tree_lock.py` holds the two equal (#3276).
"""

TREE_LOCK = "/var/lock/server-git-tree.lock"
