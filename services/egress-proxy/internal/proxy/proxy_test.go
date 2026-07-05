package proxy

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	neturl "net/url"
	"strings"
	"sync"
	"testing"

	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/config"
)

func newTestHandler(t *testing.T, cfg *config.Config, lookup LookupFunc, dial DialFunc) (*Handler, *strings.Builder) {
	t.Helper()
	var buf strings.Builder
	h := NewHandlerForTest(cfg, audit.NewLogger(&buf), lookup, dial)
	return h, &buf
}

func fixedLookup(ips ...string) LookupFunc {
	parsed := make([]net.IP, 0, len(ips))
	for _, s := range ips {
		parsed = append(parsed, net.ParseIP(s))
	}
	return func(host string) ([]net.IP, error) {
		return parsed, nil
	}
}

// startEchoListener starts a local TCP listener that echoes back a fixed
// response and returns its address.
func startEchoListener(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				buf := make([]byte, 1024)
				n, _ := c.Read(buf)
				c.Write([]byte("echo:"))
				c.Write(buf[:n])
			}(conn)
		}
	}()
	t.Cleanup(func() { ln.Close() })
	return ln.Addr().String()
}

func TestConnectDeniedForLoopback(t *testing.T) {
	echoAddr := startEchoListener(t)
	_, port, _ := net.SplitHostPort(echoAddr)

	h, auditBuf := newTestHandler(t, &config.Config{}, fixedLookup("127.0.0.1"), (&net.Dialer{}).DialContext)

	srv := httptest.NewServer(h)
	defer srv.Close()

	conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()

	req := "CONNECT internal.example:" + port + " HTTP/1.1\r\nHost: internal.example:" + port + "\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatalf("write connect: %v", err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
	if !strings.Contains(auditBuf.String(), `"decision":"block"`) {
		t.Fatalf("expected block decision in audit log: %s", auditBuf.String())
	}
	if strings.Contains(auditBuf.String(), "Authorization") {
		t.Fatalf("audit log must not contain header values: %s", auditBuf.String())
	}
}

func TestConnectAllowedForPublicIP(t *testing.T) {
	echoAddr := startEchoListener(t)

	// Resolver claims a public IP, but the injected dialer redirects any
	// dial to the local echo listener regardless of address -- this
	// simulates "public IP" resolution while keeping the test hermetic.
	// The CONNECT request itself targets port 443 (the port policy check
	// happens on the CONNECT request's port, independent of where the
	// injected dialer actually connects).
	dial := func(ctx context.Context, network, address string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, echoAddr)
	}
	h, auditBuf := newTestHandler(t, &config.Config{}, fixedLookup("93.184.216.34"), dial)

	srv := httptest.NewServer(h)
	defer srv.Close()

	conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()

	req := "CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatalf("write connect: %v", err)
	}
	br := bufio.NewReader(conn)
	line, err := br.ReadString('\n')
	if err != nil {
		t.Fatalf("read status line: %v", err)
	}
	if !strings.Contains(line, "200") {
		t.Fatalf("expected 200 Connection Established, got %q", line)
	}
	if !strings.Contains(auditBuf.String(), `"decision":"allow"`) {
		t.Fatalf("expected allow decision in audit log: %s", auditBuf.String())
	}
}

func TestConnectDeniesAnthropicHostname(t *testing.T) {
	h, _ := newTestHandler(t, &config.Config{DenyHosts: []string{"api.anthropic.com"}}, fixedLookup("93.184.216.34"), (&net.Dialer{}).DialContext)

	srv := httptest.NewServer(h)
	defer srv.Close()

	conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()

	req := "CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n"
	conn.Write([]byte(req))
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403 for api.anthropic.com, got %d", resp.StatusCode)
	}
}

func TestConnectDeniesNonStandardPort(t *testing.T) {
	h, _ := newTestHandler(t, &config.Config{}, fixedLookup("93.184.216.34"), (&net.Dialer{}).DialContext)
	srv := httptest.NewServer(h)
	defer srv.Close()

	conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()

	req := "CONNECT public.example:8443 HTTP/1.1\r\nHost: public.example:8443\r\n\r\n"
	conn.Write([]byte(req))
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403 for port 8443, got %d", resp.StatusCode)
	}
}

func TestAllowHostsRestrictsToAllowlist(t *testing.T) {
	cfg := &config.Config{AllowHosts: []string{"example.com"}}
	h, _ := newTestHandler(t, cfg, fixedLookup("93.184.216.34"), (&net.Dialer{}).DialContext)
	srv := httptest.NewServer(h)
	defer srv.Close()

	dial := func(host string) string {
		conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
		if err != nil {
			t.Fatalf("dial proxy: %v", err)
		}
		defer conn.Close()
		req := "CONNECT " + host + ":443 HTTP/1.1\r\nHost: " + host + ":443\r\n\r\n"
		conn.Write([]byte(req))
		resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
		if err != nil {
			t.Fatalf("read response: %v", err)
		}
		return resp.Status
	}

	if status := dial("notallowed.example"); !strings.Contains(status, "403") {
		t.Fatalf("expected 403 for non-allowlisted host, got %s", status)
	}
	if status := dial("api.example.com"); strings.Contains(status, "403") {
		t.Fatalf("expected allowlisted subdomain to pass policy (not necessarily connect), got %s", status)
	}
}

func TestHealthz(t *testing.T) {
	h, _ := newTestHandler(t, &config.Config{}, fixedLookup(), (&net.Dialer{}).DialContext)
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("get healthz: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestNonProxyNonHealthzRequestIsBadRequest(t *testing.T) {
	h, _ := newTestHandler(t, &config.Config{}, fixedLookup(), (&net.Dialer{}).DialContext)
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/some/path", "text/plain", strings.NewReader("x"))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", resp.StatusCode)
	}
}

func TestAbsoluteURIForwardWorksAgainstPublicSimulation(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("hello from upstream"))
	}))
	defer upstream.Close()

	// Resolver claims a public IP for "public.example"; the injected dialer
	// ignores the target address and always dials the upstream httptest
	// server instead, so the forwarded request lands on a real HTTP server.
	dial := func(ctx context.Context, network, address string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, strings.TrimPrefix(upstream.URL, "http://"))
	}
	cfg := &config.Config{}
	h, _ := newTestHandler(t, cfg, fixedLookup("93.184.216.34"), dial)

	srv := httptest.NewServer(h)
	defer srv.Close()

	proxyURL, err := neturl.Parse(srv.URL)
	if err != nil {
		t.Fatalf("parse proxy url: %v", err)
	}
	// A real forward-proxy client issues absolute-form request lines to the
	// proxy; http.Transport with Proxy set does this automatically (a plain
	// req.Write(conn) would not, since it always writes origin-form).
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}

	resp, err := client.Get("http://public.example/path")
	if err != nil {
		t.Fatalf("get via proxy: %v", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	if !strings.Contains(string(body), "hello from upstream") {
		t.Fatalf("unexpected body: %s", body)
	}
}

func TestAuditLogNeverContainsHeaderValues(t *testing.T) {
	h, auditBuf := newTestHandler(t, &config.Config{}, fixedLookup("10.0.0.5"), (&net.Dialer{}).DialContext)
	srv := httptest.NewServer(h)
	defer srv.Close()

	conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()

	req := "CONNECT private.example:443 HTTP/1.1\r\nHost: private.example:443\r\nAuthorization: Bearer super-secret-token\r\n\r\n"
	conn.Write([]byte(req))
	http.ReadResponse(bufio.NewReader(conn), nil)

	if strings.Contains(auditBuf.String(), "super-secret-token") {
		t.Fatalf("audit log leaked a header value: %s", auditBuf.String())
	}
}

func TestConcurrentConnectsDoNotRace(t *testing.T) {
	echoAddr := startEchoListener(t)
	h, _ := newTestHandler(t, &config.Config{}, fixedLookup("93.184.216.34"), func(ctx context.Context, network, address string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, echoAddr)
	})
	srv := httptest.NewServer(h)
	defer srv.Close()

	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			conn, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
			if err != nil {
				t.Errorf("dial proxy: %v", err)
				return
			}
			defer conn.Close()
			req := "CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n"
			conn.Write([]byte(req))
			http.ReadResponse(bufio.NewReader(conn), nil)
		}()
	}
	wg.Wait()
}
