# Email-to-RSS (Cloudflare Worker)

Converts email newsletters to RSS feeds. Runs as a Cloudflare Worker (not a Docker container).
Tracked as a **git submodule** at `~/server/Email-to-RSS` (upstream
<https://github.com/yl8976/Email-to-RSS>, pinned to a known-good commit in `.gitmodules`).
Admin UI at <https://email-rss.daniel-hunter.com/admin>.

`wrangler.toml` (KV namespace ids, routes) is ignored by the submodule's own `.gitignore`
and stays local-only — recreate it from `wrangler-example.toml` + step 5 below on a fresh
machine. To pull upstream changes: `cd Email-to-RSS && git pull`, redeploy, then commit the
new submodule pointer here. If local patches are ever needed, fork upstream and re-point
the submodule URL at the fork.

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

**Known limitation:** The DOMAIN variable controls both email addresses and RSS feed URLs. Since the
Worker is deployed on a subdomain (email-rss.daniel-hunter.com) but email must be received at the root
domain (daniel-hunter.com), these can't be the same value. DOMAIN is set to daniel-hunter.com so email
addresses are correct. When copying RSS feed URLs from the admin UI, manually replace daniel-hunter.com
with email-rss.daniel-hunter.com (e.g. `https://email-rss.daniel-hunter.com/rss/{feedId}`).

**Redeploying after changes:**

1. `cd ~/server/Email-to-RSS`
2. `npm run deploy`
