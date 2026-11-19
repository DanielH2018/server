"""How GitHub names Renovate, the one account the Renovate components act for.

GitHub spells one GitHub App two ways. `gh pr list --json author` and search's `author:`
qualifier name it `app/renovate`. The REST and activity APIs name its bot user
`renovate[bot]`, in a PR's `user`, an event's `actor` and a status's `creator`. A `[bot]`
login is reserved to its app, so either spelling identifies Renovate and nothing else.

The renovate-agent lander merges what `is_renovate` admits, so this module is on the
lander's approval list (`hosts/components/claude-code/templates/claude-land-approval-paths.j2`).
"""

GH_AUTHOR = "app/renovate"
BOT_LOGIN = "renovate[bot]"
_LOGINS = frozenset({GH_AUTHOR, BOT_LOGIN})


def login(account: dict | None) -> str | None:
    """The `login` of a GitHub account object, or None when the object or its login is absent."""
    return (account or {}).get("login")


def is_renovate(account: dict | None) -> bool:
    """Whether `account`, a PR's author or user, an event's actor or a status's creator, is Renovate."""
    return login(account) in _LOGINS
