// Turn the generators' <span class="fqdn" data-host="sonarr.local"> placeholders into real
// links, using the domain of the URL this page was served from. The same derivation resolves
// the nav's off-site links, which carry a sentinel host for the same reason.
//
// WHY NOT BAKE THE DOMAIN INTO THE PAGES. The generators parse the Ansible tree statically
// and `domain` is SOPS-sourced, so they cannot know it. Deriving it here is also strictly
// better than a baked value would be: the docs site answers on both docs.local.<domain> and
// docs.<domain>, and a reader gets links on the same tier they are already browsing. A LAN
// reader following a public link would leave the LAN for no reason; a public reader
// following a .local link would get nothing at all.
//
// The span's own text is the placeholder form, so a page read outside this site -- in the
// repo, on GitHub -- still says what it always said, and any failure here leaves that text
// untouched rather than producing a broken href.

(function () {
  // The docs site's own hostname is "<label>.<domain>" or "<label>.local.<domain>".
  // Dropping the first label, and "local" if it follows, leaves the domain.
  function domainFromLocation(hostname) {
    // An IP literal has no domain to recover, and dropping its first octet would produce a
    // plausible-looking string ("0.0.1") rather than an obvious failure. mkdocs serve and a
    // direct ClusterIP dial both land here.
    if (/^[0-9.]+$/.test(hostname) || hostname.indexOf(":") !== -1) {
      return null;
    }
    var labels = hostname.split(".");
    if (labels.length < 3) {
      return null;
    }
    labels.shift();
    if (labels[0] === "local") {
      labels.shift();
    }
    // Below two labels there is no registrable domain left, which means the hostname was
    // not one of ours and the guesses above do not apply.
    return labels.length >= 2 ? labels.join(".") : null;
  }

  function linkify(domain) {
    var spans = document.querySelectorAll("span.fqdn[data-host]");
    for (var i = 0; i < spans.length; i++) {
      var span = spans[i];
      var host = span.getAttribute("data-host");
      if (!host) {
        continue;
      }
      var fqdn = host + "." + domain;
      var link = document.createElement("a");
      link.href = "https://" + fqdn;
      link.textContent = fqdn;
      // These leave the docs site for a different service, and several of them are
      // long-running UIs someone will want to keep open next to the page they found it on.
      link.target = "_blank";
      link.rel = "noopener";
      span.replaceWith(link);
    }
  }

  // Is the reader on the LAN tier (docs.local.<domain>) or the public one (docs.<domain>)?
  // Sentinel links are rewritten onto whichever tier they are already browsing, for the same
  // reason the spans above are: a LAN reader sent to the public name leaves the LAN for
  // nothing, and a public reader sent to a .local name gets nothing at all.
  function isLocalTier(hostname) {
    return hostname.split(".")[1] === "local";
  }

  // mkdocs.yml can name an off-site service in the nav, but not its domain -- `domain` is
  // SOPS-sourced and the nav is a static file. Such an entry carries the sentinel host
  // `<service>.local.invalid`, which this resolves. `.invalid` is reserved by RFC 2606 and
  // resolves nowhere, so an unrewritten link fails visibly instead of reaching a stranger.
  function resolveSentinelLinks(domain, local) {
    var links = document.querySelectorAll('a[href*=".local.invalid"]');
    for (var i = 0; i < links.length; i++) {
      var link = links[i];
      var labels = link.hostname.split(".");
      if (labels.length !== 3 || labels[1] !== "local" || labels[2] !== "invalid") {
        continue;
      }
      link.hostname = labels[0] + (local ? ".local." : ".") + domain;
    }
  }

  function run() {
    var domain = domainFromLocation(window.location.hostname);
    if (domain) {
      linkify(domain);
      resolveSentinelLinks(domain, isLocalTier(window.location.hostname));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
