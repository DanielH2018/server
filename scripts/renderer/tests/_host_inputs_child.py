"""The subprocess of the `HOST_INPUTS` audit: one host render, recording every file it opens.

`test_renderer_host_inputs.py` runs this file once. Before it imports the renderer, it installs
an audit hook that records the real path of every `open`. It refuses nothing, unlike
`_render_audit_child.py`'s, because the test judges the record afterwards. The hook lives in a
subprocess because Python cannot remove an audit hook, and one installed in an xdist worker
would outlive the test.

Its one argument is a JSON object naming the host, the output directory and the file the
record is written to. The file imports only the standard library, so nothing from the repo
loads before the hook. It needs `scripts/` on `PYTHONPATH` to import the renderer.
"""

import json
import os
import sys
from pathlib import Path


def _main(spec: dict) -> int:
    opened: set[str] = set()

    def hook(event: str, args: tuple) -> None:
        if event != "open" or isinstance(args[0], int) or args[0] is None:
            return
        opened.add(os.path.realpath(os.fsdecode(args[0])))

    sys.addaudithook(hook)
    from renderer import homelab_render

    rc = homelab_render.packages(Path(spec["out"]), spec["host"], None)
    record = {"rc": rc, "opened": sorted(opened)}
    Path(spec["record"]).write_text(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(_main(json.loads(sys.argv[1])))
