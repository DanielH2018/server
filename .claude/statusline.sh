#!/bin/bash
# Homelab-aware statusLine. Renders one line:
#   <model> · <ctx%> · <dir> · <branch ↑unpushed ✚dirty>
#
# The ↑unpushed count is the point: this repo's workflow accrues local commits on
# master that wait to be pushed (reviews land as local commits first), so "how many
# commits am I sitting on" is the question worth answering at a glance. ctx% is the
# context-window fill (handy during long review/loop sessions). Reads the session JSON
# on stdin (model/dir/context/effort via jq), runs git ONCE (porcelain=v2), and degrades
# gracefully outside a repo. ANSI colors require a capable terminal.
input=$(cat)

IFS=$'\t' read -r MODEL DIRPATH CTX EFFORT < <(
  jq -r '[.model.display_name,
          .workspace.current_dir,
          ((.context_window.used_percentage // 0) | floor),
          (.effort.level // "")] | @tsv' <<<"$input"
)
DIRPATH=${DIRPATH:-$PWD}

c() { printf '\033[%sm%s\033[0m' "$1" "$2"; }   # c <ansi-code> <text>

# model (+ effort when non-default)
seg="$(c '1' "$MODEL")"
[ -n "$EFFORT" ] && [ "$EFFORT" != "medium" ] && seg+="$(c '2' " $EFFORT")"

# context fill — green < 50, yellow < 80, red beyond
if   [ "${CTX:-0}" -lt 50 ]; then ccol=32
elif [ "${CTX:-0}" -lt 80 ]; then ccol=33
else                              ccol=31; fi
sep="$(c '2' ' · ')"
seg+="${sep}$(c "$ccol" "${CTX:-0}% ctx")"

# directory basename
seg+="${sep}$(c '34' "${DIRPATH##*/}")"

# git — one porcelain=v2 call yields branch, ahead/behind, and changed entries
if info=$(git -C "$DIRPATH" status --branch --porcelain=v2 2>/dev/null); then
  branch=$(awk '/^# branch.head /{print $3}' <<<"$info")
  ahead=$(awk '/^# branch.ab /{sub(/^\+/,"",$3); print $3}' <<<"$info")
  dirty=$(grep -cE '^[^#]' <<<"$info")
  gcol=32; [ "${dirty:-0}" -gt 0 ] && gcol=33      # yellow when working tree dirty
  gitseg="$(c "$gcol" "${branch:-?}")"
  [ "${ahead:-0}" -gt 0 ] && gitseg+="$(c '36' " ↑$ahead")"
  [ "${dirty:-0}" -gt 0 ] && gitseg+="$(c '33' " ✚$dirty")"
  seg+="${sep}${gitseg}"
fi

printf '%s\n' "$seg"
