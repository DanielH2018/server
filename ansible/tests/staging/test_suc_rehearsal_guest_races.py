"""The SUC rehearsal's guest script against a fresh k3s that is still creating its own objects.

A fresh k3s answers the API before its controllers finish: the token controller adds each
namespace's `default` ServiceAccount seconds later, a just-applied CRD is not served until it is
Established, and a hostNetwork registry Pod reads Ready before it listens. The 2026-10-24 18:34
rehearsal died on the first of these. Each test below runs the real script under bash against a
stub `k3s` that raises one race for the first N calls and then lets the call through, and one test
shows an error that is not a race is not retried.

Run: uv run pytest ansible/tests/staging/test_suc_rehearsal_guest_races.py
"""

import subprocess
from types import SimpleNamespace

from _fence_probe import HYPERVISOR
from lib.proc_testing import fake_bin, path_with

SCRIPT = HYPERVISOR / "files" / "suc-rehearsal-guest-run.sh"
GUEST_IP = "10.0.0.9"

# Counters live in $T. A kubectl call is logged as `k3s <args>`, and an `apply -f -` is logged with
# the kind it carries, so a test reads the order of events from `calls`.
K3S = """\
#!/bin/bash
args="$*"
echo "k3s $args" >>"$T/calls"
bump() { local n=$(( $(cat "$T/$1" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$T/$1"; echo "$n"; }
manifest=""
[[ "$args" == *"apply"*" -f -"* ]] && manifest="$(cat)"
case "$args" in
  *"get serviceaccount default"*)
    n=$(bump sa)
    if (( n <= SA_AFTER )); then
      echo 'Error from server (NotFound): serviceaccounts "default" not found' >&2; exit 1
    fi
    echo serviceaccount/default ;;
  *"apply"*)
    kind="$(sed -n 's/^kind: //p' <<<"$manifest" | head -1)"
    [[ "$args" == *kustomization.yaml* ]] && kind=Kustomization
    [[ "$args" == *components.yaml* ]] && kind=Components
    echo "apply $kind" >>"$T/calls"
    case "$kind" in
      Pod)
        if [[ -n "$HARD" ]]; then echo 'error: invalid Pod spec' >&2; exit 1; fi
        if (( SA_AFTER > 0 && $(cat "$T/sa" 2>/dev/null || echo 0) <= SA_AFTER )); then
          echo 'Error from server (Forbidden): error looking up service account kube-system/default: serviceaccount "default" not found' >&2
          exit 1
        fi ;;
      Components)
        if (( $(bump ns) <= NS_AFTER )); then
          echo 'Error from server (NotFound): namespaces "flux-system" not found' >&2; exit 1
        fi ;;
      OCIRepository | Kustomization)
        if (( $(bump crd) <= CRD_AFTER )); then
          echo "error: no matches for kind \\"$kind\\" in version \\"x/v1\\"" >&2; exit 1
        fi ;;
    esac
    echo "$kind applied" ;;
esac
exit 0
"""
# The registry answers on the REG_AFTER+1th probe; `flux push` fails until it has,
# when REG_AFTER asks for that race.
CURL = """\
#!/bin/bash
n=$(( $(cat "$T/curl" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$T/curl"
(( n > REG_AFTER )) || exit 7
touch "$T/registry_up"
"""
FLUX = """\
#!/bin/bash
(( REG_AFTER == 0 )) || [[ -e "$T/registry_up" ]] || { echo "connection refused" >&2; exit 1; }
echo '{"digest": "sha256:0abc"}'
"""
STUBS = {
    "id": "#!/bin/bash\necho 0\n",
    "k3s": K3S,
    "curl": CURL,
    "flux": FLUX,
}


def guest(tmp_path, *argv, **knobs):
    """Run `suc-rehearsal-guest-run ARGV` against the stubs; knobs set the races (default none)."""
    stage = tmp_path / "stage"
    (stage / "hand").mkdir(parents=True, exist_ok=True)
    (stage / "hand" / "components.yaml").write_text("kind: Namespace\n")
    (stage / "hand" / "kustomization.yaml").write_text("kind: Kustomization\n")
    env = {
        "T": str(tmp_path),
        "SUC_STAGE": str(stage),
        "SUC_POLL_S": "0.05",
        "SUC_RACE_WAIT_S": "30",
        "SA_AFTER": "0",
        "NS_AFTER": "0",
        "CRD_AFTER": "0",
        "REG_AFTER": "0",
        "HARD": "",
    }
    env |= {k.upper(): str(v) for k, v in knobs.items()}
    env["PATH"] = path_with(fake_bin(tmp_path / "bin", **STUBS))
    done = subprocess.run(
        ["bash", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    calls = tmp_path / "calls"
    return SimpleNamespace(
        returncode=done.returncode,
        stdout=done.stdout,
        stderr=done.stderr,
        calls=calls.read_text().splitlines() if calls.exists() else [],
    )


def base(tmp_path, **knobs):
    return guest(tmp_path, "base", "registry:2", **knobs)


def test_the_registry_pod_is_applied_only_once_the_default_serviceaccount_exists(
    tmp_path,
):
    done = base(tmp_path, sa_after=3)
    assert done.returncode == 0, done.stderr
    gets = [c for c in done.calls if "get serviceaccount default" in c]
    assert len(gets) == 4
    assert done.calls.count("apply Pod") == 1


def test_a_serviceaccount_that_never_appears_fails_with_its_own_message(tmp_path):
    done = base(tmp_path, sa_after=10**9, suc_race_wait_s=1)
    assert done.returncode == 1
    assert "serviceaccount/default never appeared in kube-system within 1s" in (
        done.stderr
    )
    assert "apply Pod" not in done.calls


def test_flux_components_are_reapplied_while_their_namespace_is_not_there_yet(
    tmp_path,
):
    done = base(tmp_path, ns_after=2)
    assert done.returncode == 0, done.stderr
    assert done.calls.count("apply Components") == 3
    waits = [c for c in done.calls if "condition=Established" in c]
    assert len(waits) == 1
    crds = waits[0].split()
    assert "crd/ocirepositories.source.toolkit.fluxcd.io" in crds
    assert "crd/kustomizations.kustomize.toolkit.fluxcd.io" in crds


def test_the_flux_objects_are_reapplied_until_their_crds_are_served(tmp_path):
    done = guest(tmp_path, "publish", GUEST_IP, crd_after=2)
    assert done.returncode == 0, done.stderr
    assert "digest=sha256:0abc" in done.stdout
    assert done.calls.count("apply OCIRepository") == 3
    assert done.calls.count("apply Kustomization") == 1


def test_flux_push_waits_for_the_registry_to_listen(tmp_path):
    done = guest(tmp_path, "publish", GUEST_IP, reg_after=3)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "curl").read_text().strip() == "4"


def test_an_error_that_is_not_a_race_is_not_retried(tmp_path):
    done = base(tmp_path, hard=1)
    assert done.returncode == 1
    assert "error: invalid Pod spec" in done.stderr
    assert "the registry pod was not applied" in done.stderr
    assert done.calls.count("apply Pod") == 1
