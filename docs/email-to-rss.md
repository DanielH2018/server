# Email-to-RSS (Cloudflare Worker)

Converts email newsletters to RSS feeds. Runs as a Cloudflare Worker (not a Docker container).
The code lives in its own repository, the fork <https://github.com/DanielH2018/Email-to-RSS>
(moved off upstream `yl8976/Email-to-RSS` so local patches have somewhere to land). This repo
carries only this page and the probe that watches the Worker.
Admin UI at <https://email-rss.daniel-hunter.com/admin>.

**The fork's CI deploys the Worker.** `.github/workflows/ci.yml` in the fork runs the `vitest`
suite and a dry-run bundle on every pull request. A push to the fork's `main` deploys with
`wrangler deploy --env production`, then checks that `/admin/login` answers. To redeploy without
a code change, run that workflow by hand from the fork's Actions tab. The fork's README
section *Continuous deployment* lists the three repository secrets the deploy job reads.

**Renovate keeps the Worker's dependencies current.** The fork's `renovate.json` groups minor
and patch updates into one PR that merges itself once CI is green and the release is 7 days
old. That merge deploys. Major updates open their own PRs and wait for review.

**`wrangler.toml` is not in either repository.** It holds the KV namespace ids and the routes.
The fork is public and gitignores the file, so the deploy job writes it from the fork's
`WRANGLER_TOML` secret. To change it (a `compatibility_date` bump, a new route), edit a local
copy and store the whole file in that secret again, then run the workflow.

**Prerequisites:** Node.js 20+, Cloudflare account, ForwardEmail account, domain managed in Cloudflare DNS.

**Initial setup (already done — for reference):**

1. Clone the fork.
2. Run `npm ci` in the clone. The fork commits `package-lock.json`; `npm ci` installs
   exactly what it records, where `npm install` may rewrite it.
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
7. Deploy: `npm run deploy`. Every later deploy runs from the fork's CI.

`ADMIN_PASSWORD` is a Worker secret stored in Cloudflare, and a deploy keeps it. To rotate it,
rerun step 6 from a local clone.

**DNS records required in Cloudflare (daniel-hunter.com):**

- MX  @  mx1.forwardemail.net  (priority 10)  — email reception via ForwardEmail
- MX  @  mx2.forwardemail.net  (priority 10)
- TXT @  v=spf1 include:spf.forwardemail.net -all
- TXT @  `forward-email=https://email-rss.daniel-hunter.com/api/inbound`  — webhook to Worker

**DOMAIN vs the Worker hostname:** email must be received at the root domain
(daniel-hunter.com) while the Worker is routed on a subdomain (email-rss.daniel-hunter.com),
so the two can't be one value. `DOMAIN` is set to daniel-hunter.com and scopes *only* the
generated email addresses. Feed URLs are not derived from it: `admin.ts` and `rss.ts`
build `site_url`/`feed_url` from the request origin, so URLs copied out of the admin UI are
already correct.

**Monitoring:** The `blackbox-exporter` scrape job probes <https://email-rss.daniel-hunter.com/admin/login>
for a 2xx, with the target `service: email-to-rss` declared in
`k8s/roles/observability/templates/prometheus.yaml.j2`. That covers "the Worker is
routed and its script runs"; it deliberately does **not** cover the ingest path (mail landing
via ForwardEmail, KV writes), which is unmonitored — a silent stop in newsletter delivery
still surfaces only as feeds going quiet.

**Two traps when checking a change:**

- `wrangler kv key list` and the other `kv` subcommands read the local simulator by default.
  Pass `--remote` to read the production namespace; without it an empty result means nothing.
- The RSS response carries `Cache-Control: max-age=1800` (`src/routes/rss.ts` in the fork), so
  a feed fetched right after a deploy can be up to 30 minutes old. Add a query string to bypass
  the cache.
