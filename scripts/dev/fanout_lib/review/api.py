"""The review pipeline's public surface: what code outside `fanout_lib/review/` may import.

The package runs a `launch --review` batch: the red phase, the implementer, the review, the fix
round and the landing (`review.py` has the phases and why). Everything else under this
directory serves `Pipeline` and is private to the package.
`scripts/tests/test_scripts_import_direction.py` refuses a production module outside it that
imports any other module here.
"""

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.review.red_gate import review_flags
from fanout_lib.review.review import STATE_DIR, Pipeline
from fanout_lib.review.review_record import CONFIDENCE_FLOOR

__all__ = ["CONFIDENCE_FLOOR", "STATE_DIR", "Pipeline", "review_flags"]
