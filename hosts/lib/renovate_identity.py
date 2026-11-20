"""How GitHub names Renovate, the one account the Renovate components act for.

GitHub spells one GitHub App two ways. `gh pr list --json author` and search's `author:`
qualifier name it `app/renovate`. The REST and activity APIs name its bot user
`renovate[bot]`, in a PR's `user`, an event's `actor` and a status's `creator`. A `[bot]`
login is reserved to its app, so either spelling identifies Renovate and nothing else.

The renovate-agent lander merges what `is_renovate` admits, so this module is on the
lander's approval list (`hosts/components/claude-code/templates/claude-land-approval-paths.j2`).
A pull request arrives in the same two dialects: `gh`'s JSON (`author`, `headRefName`) and
REST's (`user`, `head.ref`). `RenovatePR` parses either into one value where the payload is
read, so no caller walks the dict for the author or the branch.
"""

from dataclasses import dataclass

GH_AUTHOR = "app/renovate"
BOT_LOGIN = "renovate[bot]"
_LOGINS = frozenset({GH_AUTHOR, BOT_LOGIN})
# Every branch Renovate opens starts with this; renovate.json sets no other `branchPrefix`.
BRANCH_PREFIX = "renovate/"


def login(account: dict | None) -> str | None:
    """The `login` of a GitHub account object, or None when the object or its login is absent."""
    return (account or {}).get("login")


def is_renovate(account: dict | None) -> bool:
    """Whether `account`, a PR's author or user, an event's actor or a status's creator, is Renovate."""
    return login(account) in _LOGINS


@dataclass(frozen=True)
class RenovatePR:
    """A pull request reduced to what says whether Renovate opened it.

    Attributes:
      number: the PR number.
      author: the author's login, or None when GitHub names no author (a deleted account).
      head_ref: the head branch's name.
    """

    number: int
    author: str | None
    head_ref: str

    @classmethod
    def from_gh(cls, pr: dict) -> "RenovatePR":
        """One `gh pr list|view --json number,author,headRefName` object.

        Raises:
          KeyError: the payload lacks `number` or `headRefName`.
        """
        return cls(int(pr["number"]), login(pr.get("author")), pr["headRefName"])

    @classmethod
    def from_rest(cls, pr: dict) -> "RenovatePR":
        """One REST `pulls` object.

        Raises:
          KeyError: the payload lacks `number` or `head.ref`.
        """
        return cls(int(pr["number"]), login(pr.get("user")), pr["head"]["ref"])

    @property
    def is_renovate(self) -> bool:
        """Whether Renovate authored the PR. The branch name proves nothing: anyone can push one."""
        return self.author in _LOGINS

    @property
    def on_renovate_branch(self) -> bool:
        """Whether the head branch carries Renovate's prefix."""
        return self.head_ref.startswith(BRANCH_PREFIX)
