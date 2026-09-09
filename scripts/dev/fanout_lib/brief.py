"""The batch brief the dispatcher hands each launched agent.

Task 7 fills in the batch-building and rendering logic; this file carries only the `Issue`
dataclass so `transport.py` and `placement.py` have something to import in Task 6.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
