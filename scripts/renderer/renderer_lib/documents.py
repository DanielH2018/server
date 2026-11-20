"""A rendered manifest's YAML documents, each read into a `K8sObject` beside itself.

`lib/k8s_objects.py` imports with the stdlib alone, because no-venv callers reach it
(`lib/stdlib_only.py`). Parsing YAML needs PyYAML, so the renderer-side step lives here.
"""

from collections.abc import Iterable

from lib import yaml_fast
from lib.k8s_objects import K8sObject


def parse_documents(texts: Iterable[str]) -> list[tuple[K8sObject, dict]]:
    """Each mapping document of the YAML streams `texts`, in order, parsed and beside itself.

    A reader that re-serialises, copies or edits a document takes the second member. A document
    that is not a mapping, such as an empty one, is skipped.
    """
    return [
        (K8sObject.from_item(doc), doc)
        for text in texts
        for doc in yaml_fast.safe_load_all(text)
        if isinstance(doc, dict)
    ]
