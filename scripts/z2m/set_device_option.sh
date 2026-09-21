#!/usr/bin/env bash
#
# set_device_option.sh — set one Zigbee2MQTT device option over MQTT and confirm it applied.
#
# Usage::
#
#     scripts/z2m/set_device_option.sh <device> <key> <value>
#     scripts/z2m/set_device_option.sh 'Aqara FP300' motion_sensitivity high
#     scripts/z2m/set_device_option.sh 'Aqara FP300' absence_delay_timer 60
#
# The four steps the z2m-device-setting skill used to spell out by hand, in one place: read
# the broker credentials out of SOPS, subscribe to the device's state topic, publish
# `{"<key>": <value>}` to `zigbee2mqtt/<device>/set`, and compare the state Z2M republishes
# against what was asked. The subscribe starts BEFORE the publish so a fast republish is not
# missed; the skill's original sequence subscribed afterwards and raced it.
#
# <value> is JSON when it parses as JSON (60, true, {"a":1}) and a string otherwise (high),
# so a number is not sent quoted. To force a string that looks like a number, quote it
# yourself: '"60"'.
#
# Exit codes:
#   0   the republished state carries <key> = <value>
#   1   the republished state carries a DIFFERENT <key> — Z2M rejected or coerced the value;
#       `kubectl -n homelab logs deploy/zigbee2mqtt --tail=40` names why
#   2   no state message within Z2M_READBACK_TIMEOUT seconds (default 20) — a battery device
#       applies on its next check-in, so this is "unconfirmed", not "rejected"
#   64  usage
#
# Environment (each has a default; the tests override them):
#   Z2M_MQTT_HOST         broker address (default: mqtt_k8s_vip from group_vars/all.yml)
#   Z2M_SECRETS_FILE      SOPS file holding mqtt_username / mqtt_password
#   Z2M_READBACK_TIMEOUT  seconds to wait for the republished state
#
# Credentials never sit in the command literal; they are decrypted into variables. `-P` is
# visible in `ps` for the instant each client runs — single-user hosts, accepted, and the
# recipe the repo has documented since the Docker days.

set -euo pipefail

usage() {
  echo "usage: $0 <device> <key> <value>" >&2
  exit 64
}

[[ $# -eq 3 ]] || usage
device=$1
key=$2
raw_value=$3
[[ -n "$device" && -n "$key" ]] || usage

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
secrets_file=${Z2M_SECRETS_FILE:-$repo_root/ansible/vars/secrets.yml}
timeout_s=${Z2M_READBACK_TIMEOUT:-20}

if [[ -z "${Z2M_MQTT_HOST:-}" ]]; then
  Z2M_MQTT_HOST=$(sed -n 's/^mqtt_k8s_vip:[[:space:]]*//p' "$repo_root/ansible/inventory/group_vars/all.yml")
fi
[[ -n "$Z2M_MQTT_HOST" ]] || { echo "set_device_option: no broker address (Z2M_MQTT_HOST unset, mqtt_k8s_vip not in group_vars)" >&2; exit 64; }

for bin in sops jq mosquitto_pub mosquitto_sub; do
  command -v "$bin" >/dev/null || { echo "set_device_option: $bin not on PATH (mosquitto-clients host package?)" >&2; exit 64; }
done

# A value that parses as JSON is sent as-is; anything else becomes a JSON string.
if value=$(jq -ce . <<<"$raw_value" 2>/dev/null); then :; else
  value=$(jq -cn --arg v "$raw_value" '$v')
fi
payload=$(jq -cn --arg k "$key" --argjson v "$value" '{($k): $v}')

u=$(sops -d --extract '["mqtt_username"]' "$secrets_file")
p=$(sops -d --extract '["mqtt_password"]' "$secrets_file")

state_file=$(mktemp)
trap 'rm -f "$state_file"' EXIT

# Subscribe first, then publish: Z2M republishes the device state right after a /set, and a
# subscriber started afterwards can miss it. -C 1 returns on the first message, -W bounds
# the wait so a silent device cannot hang the caller.
mosquitto_sub -h "$Z2M_MQTT_HOST" -u "$u" -P "$p" \
  -t "zigbee2mqtt/$device" -C 1 -W "$timeout_s" >"$state_file" 2>/dev/null &
sub_pid=$!
sleep 0.5

mosquitto_pub -h "$Z2M_MQTT_HOST" -u "$u" -P "$p" \
  -t "zigbee2mqtt/$device/set" -m "$payload"

wait "$sub_pid" || true

if [[ ! -s "$state_file" ]]; then
  echo "set_device_option: published $payload to zigbee2mqtt/$device/set but no state came back within ${timeout_s}s — unconfirmed (a battery device applies on its next check-in)" >&2
  exit 2
fi

if jq -e --arg k "$key" --argjson v "$value" '.[$k] == $v' "$state_file" >/dev/null; then
  echo "set_device_option: zigbee2mqtt/$device $key = $value (confirmed)"
  exit 0
fi

got=$(jq -c --arg k "$key" '.[$k]' "$state_file" 2>/dev/null || cat "$state_file")
echo "set_device_option: asked $key = $value, Z2M republished $key = $got — rejected or coerced; kubectl -n homelab logs deploy/zigbee2mqtt --tail=40 names why" >&2
exit 1
