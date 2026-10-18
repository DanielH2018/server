#!/usr/bin/bash
# /usr/local/sbin/homelab-takeover-check: the D5 flip PR's pre-merge dry-run checks, run as root
# through one NOPASSWD sudoers line (issue #5494). Installed by tasks/takeover_check.yml.
#
# Usage: homelab-takeover-check --label L --render DIR --src DIR --tags a,b[,...]
#
#   --label   output label, usually the PR number: ^[A-Za-z0-9_.-]{1,64}$, never . or ..
#   --render  `homelab_render.py cluster --out DIR` of the PR head; must resolve under the home
#             of the user who ran sudo
#   --src     a checkout of the PR head, for takeover_census.py; same rule
#   --tags    the tags the PR flips, comma separated, `<tag>-snapshot`, `-verify` and `-deferred`
#             directories included
#
# Exit codes: 0 every step ran; 1 a step failed; 2 an argument was refused; 3 the host lacks a
# binary or the kubeconfig. Nothing is persisted on the cluster: every kubectl and flux call
# below is a read or a dry run, and its flags are written out in this file, never taken from the
# caller.
#
# What root executes: /usr/local/bin/k3s, the root-owned flux copy, and the coreutils named
# below. Root never runs anything the invoking user can write, and never runs kustomize over
# the user's files: `kubectl kustomize` builds the tag as the user, and root diffs only the
# built output. The census, which is the user's Python, runs as the user through runuser.
#
# What this prints: object names, field paths and exit codes. Full outputs go to
# /var/lib/homelab-takeover-check/<label>/, mode 0700.
set -u
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset KUBECTL_EXTERNAL_DIFF KUBECONFIG KUBECACHEDIR KUBE_EDITOR EDITOR VISUAL PAGER

KUBECTL=(/usr/local/bin/k3s kubectl)
FLUX=/usr/local/lib/homelab-takeover-check/flux
STATE=/var/lib/homelab-takeover-check
KUBECONFIG_FILE=/etc/rancher/k3s/k3s.yaml
UV=uv
USER_KUBECTL=kubectl

# DECIDED: the paths above can be overridden by a test, and only when this is not root. Under
# sudo the process is root and the environment is reset, so a caller can neither name its own
# kubectl nor its own state directory; as an ordinary user the override reaches nothing the
# user could not already run. ansible/roles/setup/claude_code/tests/test_takeover_check.py holds
# the root case.
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  [ -n "${HOMELAB_TC_KUBECTL:-}" ] && KUBECTL=("$HOMELAB_TC_KUBECTL")
  [ -n "${HOMELAB_TC_FLUX:-}" ] && FLUX=$HOMELAB_TC_FLUX
  [ -n "${HOMELAB_TC_STATE:-}" ] && STATE=$HOMELAB_TC_STATE
  [ -n "${HOMELAB_TC_KUBECONFIG:-}" ] && KUBECONFIG_FILE=$HOMELAB_TC_KUBECONFIG
  [ -n "${HOMELAB_TC_UV:-}" ] && UV=$HOMELAB_TC_UV
  [ -n "${HOMELAB_TC_USER_KUBECTL:-}" ] && USER_KUBECTL=$HOMELAB_TC_USER_KUBECTL
  as_user() { "$@"; }
else
  as_user() { runuser -u "$SUDO_USER" -- "$@"; }
fi

refuse() {
  echo "refused: $1" >&2
  exit 2
}

# A lone -h or --help prints the usage and touches nothing.
if [ $# -eq 1 ] && { [ "$1" = -h ] || [ "$1" = --help ]; }; then
  sed -n '2,/^set -u/{/^set -u/!s/^# \{0,1\}//p}' "$0"
  exit 0
fi

# ── Arguments: exactly four flags, each once ────────────────────────────────────────────────
label="" render="" src="" tags=""
have_label=0 have_render=0 have_src=0 have_tags=0
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || refuse "$1 needs a value"
  case "$1" in
    --label)  [ "$have_label" -eq 0 ]  || refuse "--label given twice";  label=$2;  have_label=1 ;;
    --render) [ "$have_render" -eq 0 ] || refuse "--render given twice"; render=$2; have_render=1 ;;
    --src)    [ "$have_src" -eq 0 ]    || refuse "--src given twice";    src=$2;    have_src=1 ;;
    --tags)   [ "$have_tags" -eq 0 ]   || refuse "--tags given twice";   tags=$2;   have_tags=1 ;;
    *) refuse "unknown argument $1" ;;
  esac
  shift 2
done
[ $((have_label + have_render + have_src + have_tags)) -eq 4 ] \
  || refuse "usage: --label L --render DIR --src DIR --tags a,b"

[[ $label =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || refuse "label must match ^[A-Za-z0-9_.-]{1,64}\$"
case "$label" in . | ..) refuse "label must not be . or .." ;; esac

case "$tags" in
  "" | ,* | *, | *,,*) refuse "empty tag in --tags" ;;
esac
IFS=',' read -r -a tag_list <<< "$tags"
[ "${#tag_list[@]}" -le 30 ] || refuse "--tags takes at most 30 tags"
for tag in "${tag_list[@]}"; do
  [[ $tag =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || refuse "tag $tag must match ^[a-z0-9][a-z0-9-]{0,62}\$"
done

# ── The invoking user's home bounds both directories ────────────────────────────────────────
case "${SUDO_USER:-}" in "" | root) refuse "run this through sudo as a non-root user" ;; esac
home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
[ -n "$home" ] || refuse "no passwd entry for $SUDO_USER"
home=$(realpath -e -- "$home") || refuse "home of $SUDO_USER does not exist"
[ "$home" != / ] || refuse "home resolves to /"

# realpath resolves every symlink, so a link that leaves the home resolves outside it.
under_home() {
  local resolved
  resolved=$(realpath -e -- "$1" 2> /dev/null) || return 1
  [[ $resolved == "$home"/* ]] || return 1
  printf '%s\n' "$resolved"
}
render=$(under_home "$render") || refuse "--render must be an existing path under $home"
src=$(under_home "$src") || refuse "--src must be an existing path under $home"
[ -d "$render" ] || refuse "--render is not a directory"
[ -d "$src" ] || refuse "--src is not a directory"

# ── Host prerequisites, before anything is written ──────────────────────────────────────────
[ -x "${KUBECTL[0]}" ] || { echo "missing binary: ${KUBECTL[0]}" >&2; exit 3; }
[ -x "$FLUX" ] || { echo "missing binary: $FLUX" >&2; exit 3; }
[ -r "$KUBECONFIG_FILE" ] || { echo "missing kubeconfig: $KUBECONFIG_FILE" >&2; exit 3; }
export KUBECONFIG=$KUBECONFIG_FILE
if [ "$(/usr/bin/id -u)" -eq 0 ]; then export HOME=/root; fi

install -d -m 0711 "$STATE" || exit 3
full="$STATE/$label"
[ ! -L "$full" ] || refuse "state path for $label is a symlink"
install -d -m 0700 "$full" || exit 3

# ── Copies: root reads the user's files once, into a tree only root can reach ───────────────
# A tree is refused if it holds anything but regular files and directories, so a symlink in the
# render cannot make root read a file outside it. The copy feeds the census, which loads plain
# manifests with -f. Kustomize never runs on it as root: the build below runs as the user.
tree_is_plain() {
  [ -z "$(find -P "$1" ! -type f ! -type d -print -quit)" ]
}
copy_tree() {
  tree_is_plain "$1" || return 1
  rm -rf -- "$2"
  mkdir -- "$2"
  cp -rP --no-preserve=all -- "$1/." "$2/" || return 1
  tree_is_plain "$2"
}
copy_file() {
  [ -f "$1" ] || return 1
  cp -P --no-preserve=all -- "$1" "$2" || return 1
  [ -f "$2" ] && [ ! -L "$2" ]
}

# The first document on stdin whose metadata.name is $1, printed whole. Top-level keys end the
# metadata block, so a nested `name:` under spec never matches.
own_document() {
  awk -v want="$1" -v q="'" '
    function flush() {
      if (name == want && !done) { printf "%s", doc; done = 1 }
      doc = ""; name = ""; inmeta = 0
    }
    /^---[[:space:]]*$/ { flush(); next }
    { doc = doc $0 "\n" }
    /^[^[:space:]#]/ { inmeta = ($0 ~ /^metadata:[[:space:]]*$/) }
    inmeta && /^  name:/ {
      v = $0
      sub(/^  name:[[:space:]]*/, "", v)
      sub(/[[:space:]]*$/, "", v)
      gsub("^[\"" q "]|[\"" q "]$", "", v)
      name = v
    }
    END { flush() }
  '
}

failed=0
echo "##### $label"
for tag in "${tag_list[@]}"; do
  if [ ! -d "$render/cluster/$tag" ]; then
    echo "=== $tag: no render at cluster/$tag; skipped"
    continue
  fi
  # A symlinked cluster/<tag> would pass tree_is_plain, so it is resolved under the home too.
  tagsrc=$(under_home "$render/cluster/$tag") || { echo "=== $tag: refused, cluster/$tag leaves $home"; failed=1; continue; }
  work="$full/$tag.path"
  if ! copy_tree "$tagsrc" "$work"; then
    echo "=== $tag: refused, cluster/$tag holds a symlink or a special file"
    failed=1
    continue
  fi

  # The kustomize build runs as the user, so a remote base or any other fetch it makes is the
  # user's own. Root writes the output into a directory only root can reach, then diffs that
  # one plain file; nothing the user controls is a kustomization root builds.
  built="$full/$tag.built"
  rm -rf -- "$built"
  mkdir -- "$built"
  as_user env -i PATH="$home/.local/bin:/usr/local/bin:/usr/bin:/bin" HOME="$home" \
    "$USER_KUBECTL" kustomize "$tagsrc" > "$built/built.yaml" 2> "$full/$tag.build.err"
  rc=$?
  if [ "$rc" -ne 0 ] || [ ! -s "$built/built.yaml" ] || [ -L "$built/built.yaml" ]; then
    echo "=== $tag: refused, the build as $SUDO_USER exited $rc or wrote nothing; see $full/$tag.build.err"
    failed=1
    continue
  fi

  # A tag's Kustomization is the document of its flux file whose metadata.name is the tag. A
  # `<tag>-snapshot`, `-verify` or `-deferred` directory has no flux file of its own: its
  # Kustomization is a later document of `flux/<base>.yaml` (#5575). With no document of that
  # name, `own` stays empty and the tag takes the kubectl stand-in below.
  fluxname=$tag
  if [ ! -e "$render/flux/$fluxname.yaml" ] && [ ! -L "$render/flux/$fluxname.yaml" ]; then
    case "$tag" in
      *-snapshot) fluxname=${tag%-snapshot} ;;
      *-verify) fluxname=${tag%-verify} ;;
      *-deferred) fluxname=${tag%-deferred} ;;
    esac
  fi
  own="$full/$tag.kustomization.yaml"
  : > "$own"
  if [ -e "$render/flux/$fluxname.yaml" ] || [ -L "$render/flux/$fluxname.yaml" ]; then
    fluxsrc=$(under_home "$render/flux/$fluxname.yaml") || fluxsrc=""
    if [ -n "$fluxsrc" ] && copy_file "$fluxsrc" "$full/$tag.flux-source.yaml"; then
      own_document "$tag" < "$full/$tag.flux-source.yaml" > "$own"
    else
      echo "=== $tag: refused, flux/$fluxname.yaml is not a regular file under $home"
      failed=1
      continue
    fi
  fi

  if grep -q 'postBuild' "$own"; then
    echo "=== $tag: flux diff kustomization"
    # Root's own kustomization over the built file: the only thing flux builds.
    printf 'resources:\n  - built.yaml\n' > "$built/kustomization.yaml"
    "$FLUX" --kubeconfig "$KUBECONFIG_FILE" diff kustomization "$tag" \
      --path "$built" --kustomization-file "$own" \
      --ignore-not-found --strict-substitute --progress-bar=false \
      > "$full/$tag.diff" 2> "$full/$tag.err"
    rc=$?
    echo "flux diff exit=$rc (0 no change, 1 changes, 2+ error)"
    if [ "$rc" -gt 1 ]; then
      failed=1
      echo "error: see $full/$tag.err"
    fi
    # Object lines start with a marker; field paths start at column 0; values are indented.
    grep -E '^(►|✓|✗|[^[:space:]])' "$full/$tag.diff" | cut -c1-160
    grep -E '^(►|✓|✗)' "$full/$tag.err" | cut -c1-200
  else
    echo "=== $tag: kubectl diff"
    "${KUBECTL[@]}" diff --server-side --field-manager=kustomize-controller --force-conflicts \
      -f "$built/built.yaml" > "$full/$tag.diff" 2> "$full/$tag.err"
    rc=$?
    echo "kubectl diff exit=$rc (0 no change, 1 changes, 2+ error)"
    if [ "$rc" -gt 1 ]; then
      failed=1
      echo "error: see $full/$tag.err"
    else
      grep -E '^\+\+\+ ' "$full/$tag.diff" | sed -E 's#^\+\+\+ ([^ \t]*/)?([^ \t/]*).*#object: \2#'
      # Keep YAML key lines only, cut at the colon. A block scalar's text lines carry no key,
      # so a ConfigMap's embedded script never reaches the output.
      grep -E '^[+-]' "$full/$tag.diff" | grep -v -E '^(\+\+\+|---)( |$)' \
        | grep -E '^[+-][[:space:]]*(- )?[A-Za-z0-9_./"-]+:([[:space:]]|$)' \
        | sed -E 's/^([+-][[:space:]]*(- )?[A-Za-z0-9_./"-]+:).*/\1/' | sort | uniq -c
    fi
  fi

  # The census reads the copy, so what root sends to the API server is what the user's own
  # census compares against.
  for f in "$work"/*.yaml; do
    [ -f "$f" ] || continue
    grep -q -E '^kind: (Cluster)?Role(Binding)?$' "$f" || continue
    name=$(basename -- "$f")
    if grep -q -E '^[[:space:]]*kind:[[:space:]]*["'\'']?Secret(List)?["'\'']?[[:space:]]*$' "$f"; then
      echo "=== $tag: RBAC census over $name skipped, the file also holds a Secret"
      continue
    fi
    echo "=== $tag: RBAC takeover census over $name"
    # Readable by the user, because the census runs as the user. RBAC objects only: a file with
    # a Secret was skipped above.
    census=$(mktemp -d -p "$STATE" .census.XXXXXX)
    chmod 0711 "$census"
    "${KUBECTL[@]}" get -f "$f" -o json --show-managed-fields \
      > "$census/live.json" 2> "$full/$tag-$name-get.err"
    rc=$?
    echo "kubectl get exit=$rc"
    if [ "$rc" -ne 0 ] && grep -q 'NotFound' "$full/$tag-$name-get.err"; then
      echo "census skipped: the file's objects are not live yet, so nothing is taken over"
      rm -r "$census"
      continue
    fi
    "${KUBECTL[@]}" apply --server-side --dry-run=server --force-conflicts \
      --field-manager=kustomize-controller -f "$f" -o json --show-managed-fields \
      > "$census/dryrun.json" 2> "$full/$tag-$name-apply.err"
    echo "kubectl apply --dry-run=server exit=$?"
    chmod 0644 "$census/live.json" "$census/dryrun.json"
    (
      cd "$src" || exit 1
      as_user env -i PATH="$home/.local/bin:/usr/local/bin:/usr/bin:/bin" HOME="$home" \
        "$UV" run --quiet python scripts/dev/takeover_census.py "$census/live.json" "$census/dryrun.json"
    )
    rc=$?
    echo "takeover_census exit=$rc"
    [ "$rc" -le 1 ] || failed=1
    rm -r "$census"
  done
done
echo "full outputs (not printed): $full"
exit "$failed"
