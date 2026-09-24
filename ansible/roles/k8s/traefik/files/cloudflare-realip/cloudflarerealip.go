// Package cloudflarerealip is a Traefik local (Yaegi) middleware plugin. For a request whose
// connection comes from a Cloudflare edge, it replaces X-Forwarded-For and X-Real-Ip with the
// client address Cloudflare reports in CF-Connecting-IP. Requests from any other source pass
// through untouched.
//
// Why: Cloudflare APPENDS the client address to whatever X-Forwarded-For the client sent, and
// the entrypoint's forwardedHeaders.trustedIPs keeps that whole chain from a Cloudflare
// source. A consumer that reads the LEFTMOST entry (Authelia) therefore logged a value the
// client chose. After this plugin the chain Traefik forwards is `client, edge`.
//
// Loaded from a ConfigMap through experimental.localPlugins (static-config.yaml.j2), so the
// edge takes no network dependency at startup for it. The traefik role's CLAUDE.md has the
// rest.
package cloudflarerealip

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"strings"
)

// Config is the middleware's configuration, set in the Middleware CRD.
type Config struct {
	// TrustedIPs are the CIDRs of the Cloudflare edge (group_vars `cloudflare_ips`).
	TrustedIPs []string `json:"trustedIPs,omitempty"`
}

// CreateConfig returns the default (empty) configuration.
func CreateConfig() *Config {
	return &Config{}
}

// RealIP is the middleware handler.
type RealIP struct {
	next    http.Handler
	name    string
	trusted []*net.IPNet
}

// New builds the middleware. An empty or unparseable CIDR list is an error rather than a
// silent pass-through, so a broken Middleware shows as a rejected router, not as an edge
// that quietly stopped rewriting.
func New(_ context.Context, next http.Handler, config *Config, name string) (http.Handler, error) {
	if len(config.TrustedIPs) == 0 {
		return nil, fmt.Errorf("%s: trustedIPs is empty", name)
	}
	trusted := make([]*net.IPNet, 0, len(config.TrustedIPs))
	for _, cidr := range config.TrustedIPs {
		_, network, err := net.ParseCIDR(strings.TrimSpace(cidr))
		if err != nil {
			return nil, fmt.Errorf("%s: invalid trustedIPs entry %q: %w", name, cidr, err)
		}
		trusted = append(trusted, network)
	}
	return &RealIP{next: next, name: name, trusted: trusted}, nil
}

func (r *RealIP) fromTrusted(req *http.Request) bool {
	host, _, err := net.SplitHostPort(req.RemoteAddr)
	if err != nil {
		host = req.RemoteAddr
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	for _, network := range r.trusted {
		if network.Contains(ip) {
			return true
		}
	}
	return false
}

func (r *RealIP) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	if r.fromTrusted(req) {
		client := strings.TrimSpace(req.Header.Get("CF-Connecting-IP"))
		if net.ParseIP(client) != nil {
			req.Header.Set("X-Forwarded-For", client)
			req.Header.Set("X-Real-Ip", client)
		} else {
			// Cloudflare always sends CF-Connecting-IP, so this branch should not run. If it
			// does, drop the client-supplied chain rather than forward it: consumers then fall
			// back to the connection address, which is the edge, not a value the client chose.
			req.Header.Del("X-Forwarded-For")
			req.Header.Del("X-Real-Ip")
		}
	}
	r.next.ServeHTTP(rw, req)
}
