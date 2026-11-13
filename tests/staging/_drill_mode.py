"""The stubs and assertions the orchestrator's two hand-run modes share in their tests.

Used by test_suc_rehearsal_mode.py and test_bootstrap_drill_mode.py. Not a test module itself.
`virsh` and `qemu-img` stand in for libvirt; `Run` reads what the stubs recorded.
"""

VIRSH = """\
#!/bin/bash
echo "virsh $*" >>"$EVENTS"
case "$3" in
  domstate) [[ -e "$T/running" ]] && echo running || exit 1 ;;
  create) touch "$T/running" ;;
  destroy) rm -f "$T/running" ;;
esac
exit 0
"""
QEMU_IMG = """\
#!/bin/bash
[[ "$1" == convert ]] && touch "${@: -1}"
exit 0
"""
LOGGER = "#!/bin/bash\nexit 0\n"


class Run:
    """One orchestrator run: its exit status and everything the stubs recorded."""

    def __init__(self, tmp_path):
        self.t = tmp_path
        self.done = None

    @property
    def events(self):
        path = self.t / "events"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def steps(self):
        return [e.split()[1] for e in self.events if e.startswith("step ")]

    def evidence(self, name):
        return (self.t / "evidence" / name).read_text()


def assert_cleaned_up(r):
    """The guest is gone, its disk is deleted, nothing was defined: the exit-path guarantee."""
    assert not (r.t / "disk.qcow2").exists(), "the guest's disk outlived the run"
    assert not (r.t / "running").exists(), "the guest was left running"
    created = any(" create " in e for e in r.events)
    destroyed = any(" destroy " in e for e in r.events)
    assert destroyed or not created, "a created guest was never destroyed"
    assert not any(" define " in e for e in r.events), "the run defined a domain"


def assert_drill_evidence_untouched(r):
    assert not (r.t / "stamp").exists(), (
        "a hand-run mode wrote the monthly drill's stamp"
    )
    assert not list((r.t / "textfile").iterdir()), (
        "a hand-run mode wrote a textfile verdict"
    )
