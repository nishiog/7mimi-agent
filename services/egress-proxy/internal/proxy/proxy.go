// Package proxy implements the egress-proxy forward proxy (ADR-025).
//
// agent-runner containers reach the public internet only through this
// proxy. Policy is evaluated on the *resolved* destination IP addresses
// (not the hostname) so that DNS answers cannot be used to bypass the
// RFC1918/loopback/link-local/ULA denylist (a classic DNS-rebinding TOCTOU).
// The proxy dials the validated IP directly; it never re-resolves the
// hostname between the check and the connect.
package proxy

import (
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/config"
)

// LookupFunc resolves a hostname to its A/AAAA records. Injectable so tests
// can simulate public IPs without relying on real DNS.
type LookupFunc func(host string) ([]net.IP, error)

// DialFunc opens a TCP connection to a validated address. Injectable so
// tests can redirect "public" addresses at local httptest/TCP listeners.
type DialFunc func(ctx context.Context, network, address string) (net.Conn, error)

// Handler is the egress-proxy forward proxy: CONNECT tunneling for HTTPS
// and absolute-URI forwarding for plain HTTP, plus /healthz.
type Handler struct {
	cfg      *config.Config
	logger   *audit.Logger
	lookupIP LookupFunc
	dial     DialFunc
}

func NewHandler(cfg *config.Config, logger *audit.Logger) *Handler {
	return &Handler{
		cfg:    cfg,
		logger: logger,
		lookupIP: func(host string) ([]net.IP, error) {
			ipAddrs, err := net.DefaultResolver.LookupIP(context.Background(), "ip", host)
			return ipAddrs, err
		},
		dial: (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
	}
}

// NewHandlerForTest builds a Handler with injectable resolver/dialer, for
// use by tests in this package and _test.go files.
func NewHandlerForTest(cfg *config.Config, logger *audit.Logger, lookupIP LookupFunc, dial DialFunc) *Handler {
	return &Handler{cfg: cfg, logger: logger, lookupIP: lookupIP, dial: dial}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	switch {
	case r.Method == http.MethodConnect:
		h.handleConnect(w, r, start)
	case r.URL.IsAbs():
		h.handleForward(w, r, start)
	case r.Method == http.MethodGet && r.URL.Path == "/healthz":
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	default:
		http.Error(w, "bad request: expected CONNECT, absolute-URI, or GET /healthz", http.StatusBadRequest)
	}
}

type decision struct {
	allowed bool
	reason  string
	ip      string
}

// evaluate applies policy to host:port, resolving host to validate the
// destination IPs. It never returns an allowed decision without an ip set.
func (h *Handler) evaluate(host, port string) decision {
	lowerHost := strings.ToLower(strings.TrimSuffix(host, "."))

	for _, deny := range h.cfg.DenyHosts {
		if lowerHost == deny {
			return decision{allowed: false, reason: "denied hostname"}
		}
	}

	if len(h.cfg.AllowHosts) > 0 {
		matched := false
		for _, allow := range h.cfg.AllowHosts {
			if lowerHost == allow || strings.HasSuffix(lowerHost, "."+allow) {
				matched = true
				break
			}
		}
		if !matched {
			return decision{allowed: false, reason: "hostname not in allowlist"}
		}
	}

	if port != "80" && port != "443" {
		return decision{allowed: false, reason: "port not allowed"}
	}

	ips, err := h.lookupIP(lowerHost)
	if err != nil || len(ips) == 0 {
		return decision{allowed: false, reason: "dns resolution failed"}
	}
	for _, ip := range ips {
		if isPrivateOrReserved(ip) {
			return decision{allowed: false, reason: "resolved to private/reserved IP"}
		}
	}

	return decision{allowed: true, reason: "", ip: ips[0].String()}
}

func isPrivateOrReserved(ip net.IP) bool {
	return ip.IsLoopback() ||
		ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified() ||
		ip.IsPrivate() // covers RFC1918 (10/8, 172.16/12, 192.168/16) and ULA fc00::/7
}

func (h *Handler) audit(method, host, port, dec, reason string, start time.Time) {
	h.logger.Log(audit.Event{
		Method:     method,
		Host:       host,
		Port:       port,
		Decision:   dec,
		Reason:     reason,
		DurationMS: time.Since(start).Milliseconds(),
	})
}

func (h *Handler) handleConnect(w http.ResponseWriter, r *http.Request, start time.Time) {
	host, port, err := net.SplitHostPort(r.Host)
	if err != nil {
		h.audit(r.Method, r.Host, "", "block", "invalid CONNECT target", start)
		http.Error(w, "invalid CONNECT target", http.StatusBadRequest)
		return
	}

	dec := h.evaluate(host, port)
	if !dec.allowed {
		h.audit(r.Method, host, port, "block", dec.reason, start)
		http.Error(w, dec.reason, http.StatusForbidden)
		return
	}

	upstream, err := h.dial(r.Context(), "tcp", net.JoinHostPort(dec.ip, port))
	if err != nil {
		h.audit(r.Method, host, port, "block", "dial failed", start)
		http.Error(w, "dial failed", http.StatusBadGateway)
		return
	}

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		upstream.Close()
		h.audit(r.Method, host, port, "block", "hijack unsupported", start)
		http.Error(w, "hijack unsupported", http.StatusInternalServerError)
		return
	}
	client, _, err := hijacker.Hijack()
	if err != nil {
		upstream.Close()
		h.audit(r.Method, host, port, "block", "hijack failed", start)
		return
	}

	h.audit(r.Method, host, port, "allow", "", start)
	client.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))

	done := make(chan struct{}, 2)
	go func() {
		io.Copy(upstream, client)
		done <- struct{}{}
	}()
	go func() {
		io.Copy(client, upstream)
		done <- struct{}{}
	}()
	<-done
	upstream.Close()
	client.Close()
}

func (h *Handler) handleForward(w http.ResponseWriter, r *http.Request, start time.Time) {
	host := r.URL.Hostname()
	port := r.URL.Port()
	if port == "" {
		if r.URL.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}

	dec := h.evaluate(host, port)
	if !dec.allowed {
		h.audit(r.Method, host, port, "block", dec.reason, start)
		http.Error(w, dec.reason, http.StatusForbidden)
		return
	}

	targetAddr := net.JoinHostPort(dec.ip, port)
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return h.dial(ctx, network, targetAddr)
		},
	}
	client := &http.Client{Transport: transport, Timeout: 30 * time.Second}

	outReq, err := http.NewRequestWithContext(r.Context(), r.Method, r.URL.String(), r.Body)
	if err != nil {
		h.audit(r.Method, host, port, "block", "failed to build outbound request", start)
		http.Error(w, "failed to build outbound request", http.StatusInternalServerError)
		return
	}
	outReq.Header = r.Header.Clone()
	outReq.Host = r.URL.Host

	resp, err := client.Do(outReq)
	if err != nil {
		h.audit(r.Method, host, port, "block", "upstream request failed", start)
		http.Error(w, "upstream request failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for key, values := range resp.Header {
		for _, v := range values {
			w.Header().Add(key, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)

	h.audit(r.Method, host, port, "allow", "", start)
}
