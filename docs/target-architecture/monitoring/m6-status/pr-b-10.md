### PR B-10

PR B-10, triage's poller drops its Kuma source:
[#6997](https://github.com/DanielH2018/server/pull/6997). It merged on 2026-11-02 at `589c932c8`.
It was held until A6's Cutover-1b was live, so that every Kuma pager had an `ALERTS` rule and the
`ALERTS` path saw each fall the Kuma path saw. [#6876](https://github.com/DanielH2018/server/pull/6876)
named the poller as a Kuma consumer.

The PR removes `KUMA_QUERY`, `KUMA_UP_QUERY`, `kuma_up_keys`, the `kuma` arm of `unknown_sources`
and of `down_keys`, and the `kuma_up` argument of `step` from
`hosts/components/claude-code/files/claude_triage_poll.py`. Recovery is now generic: a key that is
not firing and whose source is not unknown recovers. The first successful poll on the new code
therefore drops every `kuma:` key in `poll-state.json` and opens no episode for it.

**Only daniel-box carries the poller.** The component's `when:` gates `claude_triage_poll.py` on
`claude_code_triage_enabled`, which `ansible/inventory/host_vars/daniel-box.yml` sets true and
`hosts/components/claude-code/defaults/main.yml` leaves false. A render of both host packages
before the change put the file in `homelab-daniel-box` and not in `homelab-daniel-server`. Item 9's
**Order** and C-1a's precondition once said "both hosts". They now say daniel-box, because
daniel-server has no poller to read the scrape job that C-1a deletes.

C-1a's precondition stays open until `land.sh` reports daniel-box applied and the journal of
`homelab-claude-triage-poll` on daniel-box shows a poll with no `uptime-kuma` query.
