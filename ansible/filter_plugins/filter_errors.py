"""`_as_filter`, the one wrapper the filter plugins register their filters through (#4527).

It re-raises a filter's `ValueError` as `AnsibleFilterError("<filter>: ...")`. Seven plugins
carried a verbatim copy after #4480 and #4505; each now imports this one.

DECIDED: the home is `ansible/filter_plugins/`, not a directory outside it. A change here
then stays a deploy-plane path, and `narrow_lib/filters.py:plugin_names` refuses to narrow it
because sibling plugins import it ("is imported by ..."). The empty `filters()` refuses there
too. Refusing is the right answer: the wrapper reaches every caller of every importer. The
setup roles those importers reach are this file's row of
`deploy_cross_role.SETUP_ROLES_CALLING_FILTER_PLUGINS`.

Ansible loads every `.py` here as a plugin, so this module carries an empty `FilterModule`.
Ansible imports a plugin by file path without putting this directory on `sys.path`, so each
importer inserts it first; the pytest `pythonpath` already holds it.
"""

import functools


def _as_filter(func):
    """`func` as Ansible calls it: a ValueError re-raised as `AnsibleFilterError`.

    The message opens with `<func's name>: `, which is the name the filter registers under.
    The prefix names the filter that refused, since a render failure otherwise reaches the
    operator as a bare message under the task's template path (#4480). The import is deferred
    so the module still loads where Ansible is not installed.
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValueError as exc:
            from ansible.errors import AnsibleFilterError

            raise AnsibleFilterError(f"{func.__name__}: {exc}") from exc

    return wrapped


class FilterModule:
    """Registers no filter: this module only holds the shared wrapper."""

    def filters(self):
        return {}
