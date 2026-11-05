### PR C-6

Tracked by [#7191](https://github.com/DanielH2018/server/issues/7191). The reads ran on 2026-11-05
at base `decdee190`, with daniel-box on `0.6776+gc0f46a5e9157`, daniel-server on
`0.6760+g46a2bc43e376` and daniel-pi on `0.6769+g7f3ec44c07c3`.

- **The absent tasks.** `ls` reports no such file for `/usr/local/lib/kuma-push-lib.sh` and
  `/var/lib/homelab/setup-deployed-manifest.d/kuma-push-lib` on all three hosts. It also reports
  no such file for `kuma_push.py` in every `/opt` consumer directory on daniel-box and
  daniel-server, read by name. `/opt/containerd` denies the `find`, and it holds no consumer.
- **The `legacy:` entries.** `ls` reports no such file for each path C-6 deletes, on every host,
  and `systemctl list-unit-files` prints no `kuma-check-loki-read-route` or
  `kuma-check-secret-rotation-audit` unit on daniel-box. Those two components ship on daniel-box
  alone. daniel-server still runs `kuma-check-loki-read-route` from the `initial_setup` role, and
  no entry C-6 deletes reaches it.
- **Exit criterion 3.** The precondition grep lists only the allowlist's identifier holders and
  the two generated records. C-4b's three entries stay until D6.

The census found far more than the plan's budget: 553 lines in 208 files, against about 100
lines. C-6 reworded what the plan assigns to it, which leaves `hosts/` holding only the Pi compose
files and rootq. The remaining 167 files sit in `_PROSE_HOLDERS`, each with its reason.
[#7194](https://github.com/DanielH2018/server/issues/7194) owns those, and
[#7195](https://github.com/DanielH2018/server/issues/7195) owns the redaction rules.
