#!/usr/bin/env bash
# Poll Oracle Cloud until an Always Free A1 shape can actually be launched, then launch it.
#
# WHY THIS EXISTS: "the shape is offered" and "capacity is obtainable" are different facts.
# us-chicago-1 listed VM.Standard.A1.Flex while every availability domain there returned
# "Out of host capacity" (2026-08-30). Capacity appears and is claimed within minutes, so a
# human refreshing the console loses to anyone running a loop.
#
# Measured the same day: asking for LESS does not help. A 1 OCPU / 6 GB request was refused
# alongside 2 OCPU / 12 GB, so the shortage is of A1 hosts outright rather than of large
# slices on them. Ask for the full allowance; there is nothing to gain by shrinking.
#
# THROWAWAY BOOTSTRAP TOOLING. It runs until it succeeds once and is then dead code — keep
# it only against the node being reclaimed or rebuilt. It is deliberately not an Ansible
# role, not a cron, and not wired into the deploy plane: it needs an OCI API signing key,
# which is a credential this repo otherwise has no reason to hold.
#
# Usage:
#   export OCI_COMPARTMENT_OCID=ocid1.tenancy.oc1..xxx
#   export OCI_SUBNET_OCID=ocid1.subnet.oc1.us-chicago-1.xxx
#   export OCI_IMAGE_OCID=ocid1.image.oc1.us-chicago-1.xxx   # Ubuntu 24.04 AARCH64
#   export OCI_SSH_PUBKEY=~/.ssh/id_ed25519.pub
#   export OCI_ADS="mQAP:US-CHICAGO-1-AD-1,mQAP:US-CHICAGO-1-AD-2,mQAP:US-CHICAGO-1-AD-3"
#   ./scripts/dev/oci_capacity_retry.sh
#
# Run it under `systemd-run --user` or tmux — it is expected to take hours or days.

set -euo pipefail

: "${OCI_COMPARTMENT_OCID:?set OCI_COMPARTMENT_OCID}"
: "${OCI_SUBNET_OCID:?set OCI_SUBNET_OCID}"
: "${OCI_IMAGE_OCID:?set OCI_IMAGE_OCID}"
: "${OCI_SSH_PUBKEY:?set OCI_SSH_PUBKEY (path to a .pub file)}"
: "${OCI_ADS:?set OCI_ADS (comma-separated availability domain names)}"

DISPLAY_NAME="${OCI_DISPLAY_NAME:-daniel-cloud}"
OCPUS="${OCI_OCPUS:-2}"
MEM_GB="${OCI_MEM_GB:-12}"
BOOT_GB="${OCI_BOOT_GB:-200}"
SLEEP_SECS="${OCI_SLEEP_SECS:-60}"

if ! command -v oci >/dev/null 2>&1; then
    echo "FATAL: the OCI CLI is not on PATH. Install and configure it first:" >&2
    echo "  https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm" >&2
    exit 127
fi

if [ ! -r "$OCI_SSH_PUBKEY" ]; then
    echo "FATAL: cannot read SSH public key at $OCI_SSH_PUBKEY" >&2
    exit 2
fi

# Split the AD list once, rather than re-parsing every cycle.
IFS=',' read -r -a ADS <<<"$OCI_ADS"

echo "Polling for ${OCPUS} OCPU / ${MEM_GB} GB A1 capacity across ${#ADS[@]} AD(s), every ${SLEEP_SECS}s."
echo "Ctrl-C to stop. Started $(date --iso-8601=seconds)."

attempt=0
while true; do
    attempt=$((attempt + 1))
    for ad in "${ADS[@]}"; do
        # --wait-for-state is deliberately omitted: we want the API's immediate verdict on
        # capacity, not a blocking wait on an instance that was never created.
        if out=$(oci compute instance launch \
            --compartment-id "$OCI_COMPARTMENT_OCID" \
            --availability-domain "$ad" \
            --shape "VM.Standard.A1.Flex" \
            --shape-config "{\"ocpus\":${OCPUS},\"memoryInGBs\":${MEM_GB}}" \
            --image-id "$OCI_IMAGE_OCID" \
            --subnet-id "$OCI_SUBNET_OCID" \
            --assign-public-ip true \
            --boot-volume-size-in-gbs "$BOOT_GB" \
            --display-name "$DISPLAY_NAME" \
            --metadata "{\"ssh_authorized_keys\":\"$(cat "$OCI_SSH_PUBKEY")\"}" \
            2>&1); then

            echo
            echo "LAUNCHED in ${ad} on attempt ${attempt} at $(date --iso-8601=seconds)."
            echo "$out"
            echo
            echo "Next: reserve the public IP (an ephemeral one does not survive a stop/start),"
            echo "then add the hosts.ini entry and run ansible/bootstrap.yml."
            exit 0
        fi

        # Capacity is the expected failure and is not worth a line of output every minute.
        # Anything else — a bad OCID, an expired key, a malformed shape config — will never
        # fix itself by retrying, so surface it and stop rather than looping silently.
        if ! printf '%s' "$out" | grep -qi "out of host capacity\|outofhostcapacity"; then
            echo >&2
            echo "FATAL: launch failed for a reason other than capacity, in ${ad}:" >&2
            echo "$out" >&2
            exit 1
        fi
    done

    printf '\rattempt %d — no capacity in any AD, last checked %s' \
        "$attempt" "$(date +%H:%M:%S)"
    sleep "$SLEEP_SECS"
done
