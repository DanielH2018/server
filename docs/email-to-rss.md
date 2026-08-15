# Email-to-RSS (Cloudflare Worker)

Converts email newsletters to RSS feeds. Runs as a Cloudflare Worker (not a Docker container).
Tracked as a **git submodule** at `~/server/Email-to-RSS`, pinned to a known-good commit.
The submodule URL points at the fork <https://github.com/DanielH2018/Email-to-RSS> (moved off
upstream `yl8976/Email-to-RSS` in `77ed09ec` so local patches have somewhere to land).
Admin UI at <https://email-rss.daniel-hunter.com/admin>.

**Deploys run from `daniel-server`.** That is the only host carrying the deploy state:
`~/server/Email-to-RSS/wrangler.toml`, `node_modules/`, and the Cloudflare credential in
`~/.config/.wrangler`. `daniel-box` has the submodule checked out but none of those and no
`wrangler` on `PATH`, so `npm run deploy` there does nothing useful. Node comes from `fnm`
in the interactive zsh — a non-interactive `ssh daniel-server npm …` won't find it.

`wrangler.toml` (KV namespace ids, routes) is ignored by the submodule's own `.gitignore`
and stays local-only — recreate it from `wrangler-example.toml` + step 5 below on a fresh
machine. To pull upstream changes: `cd Email-to-RSS && git pull`, redeploy, then commit the
new submodule pointer here.

**Prerequisites:** Node.js 20+, Cloudflare account, ForwardEmail account, domain managed in Cloudflare DNS.

**Initial setup (already done — for reference):**

1. Fetch the code: `git submodule update --init Email-to-RSS` (originally a plain clone of the repo above)
2. Run `npm install` in the repo directory.
3. Authenticate with Cloudflare: `npx wrangler login`
4. Create KV namespaces manually (setup.sh has a bug with namespace title matching):
   `npx wrangler kv namespace create EMAIL_STORAGE`
   `npx wrangler kv namespace create EMAIL_STORAGE --preview`
5. Copy wrangler-example.toml to wrangler.toml and fill in:
   - compatibility_date: today's date (YYYY-MM-DD)
   - KV namespace IDs from step 4
   - DOMAIN: daniel-hunter.com
   - routes: email-rss.daniel-hunter.com (subdomain required — root domain has existing A records)
6. Set admin password: `npx wrangler secret put ADMIN_PASSWORD --env production` (confirm worker creation when prompted)
7. Deploy: `npm run deploy`

**DNS records required in Cloudflare (daniel-hunter.com):**

- MX  @  mx1.forwardemail.net  (priority 10)  — email reception via ForwardEmail
- MX  @  mx2.forwardemail.net  (priority 10)
- TXT @  v=spf1 include:spf.forwardemail.net -all
- TXT @  `forward-email=https://email-rss.daniel-hunter.com/api/inbound`  — webhook to Worker

**DOMAIN vs the Worker hostname:** email must be received at the root domain
(daniel-hunter.com) while the Worker is routed on a subdomain (email-rss.daniel-hunter.com),
so the two can't be one value. `DOMAIN` is set to daniel-hunter.com and now scopes *only* the
generated email addresses. Feed URLs are no longer derived from it — `admin.ts` and `rss.ts`
build `site_url`/`feed_url` from the request origin, so URLs copied out of the admin UI are
already correct. (Before commit `fb7f2e9` they weren't, and this section told you to
hand-edit them; that step is obsolete.)

**Monitoring:** Uptime Kuma probes <https://email-rss.daniel-hunter.com/admin/login> for a
200 every 5 min — monitor id `email-to-rss`, declared in
`ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`. That covers "the Worker is
routed and its script runs"; it deliberately does **not** cover the ingest path (mail landing
via ForwardEmail, KV writes), which is unmonitored — a silent stop in newsletter delivery
still surfaces only as feeds going quiet.

**Redeploying after changes** (on `daniel-server`):

1. `cd ~/server/Email-to-RSS`
2. `npm run deploy`
3. Commit the new submodule pointer in `~/server` if the checkout moved.
