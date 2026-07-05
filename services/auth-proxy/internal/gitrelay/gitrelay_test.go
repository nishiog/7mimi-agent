package gitrelay

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/githubapp"
)

func testTokenSource(t *testing.T, upstream *httptest.Server) *githubapp.TokenSource {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating key: %v", err)
	}
	appAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/app/installations":
			w.Write([]byte(`[{"id":1}]`))
		case strings.Contains(r.URL.Path, "/access_tokens"):
			resp := map[string]string{
				"token":      "install-token-abc",
				"expires_at": time.Now().Add(1 * time.Hour).UTC().Format(time.RFC3339),
			}
			body, _ := json.Marshal(resp)
			w.Write(body)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(appAPI.Close)
	return githubapp.NewTokenSource("app-id", "", key, appAPI.URL)
}

func newTestHandler(t *testing.T, sessionToken string, upstream *httptest.Server) *Handler {
	t.Helper()
	tokens := testTokenSource(t, upstream)
	h, err := NewHandler(sessionToken, tokens, upstream.URL, audit.NewLogger(io.Discard))
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	return h
}

func TestNewHandlerRejectsEmptySessionToken(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer upstream.Close()
	tokens := testTokenSource(t, upstream)
	if _, err := NewHandler("", tokens, upstream.URL, audit.NewLogger(io.Discard)); err == nil {
		t.Fatal("expected error for empty session token")
	}
}

func TestRelayUnauthorizedWithoutBearer(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("upstream should not be reached without valid auth")
	}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

func TestRelayUnauthorizedWithWrongBearer(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("upstream should not be reached with wrong token")
	}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer wrong-token")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

func TestRelayUpstreamSeesBasicAuthNotClientBearer(t *testing.T) {
	var gotAuth string
	var gotPath string
	var gotProtocol string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		gotProtocol = r.Header.Get("Git-Protocol")
		w.Header().Set("Content-Type", "application/x-git-upload-pack-advertisement")
		w.Write([]byte("ok"))
	}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	req.Header.Set("Git-Protocol", "version=2")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body=%s", rec.Code, rec.Body.String())
	}

	wantAuth := "Basic " + base64.StdEncoding.EncodeToString([]byte("x-access-token:install-token-abc"))
	if gotAuth != wantAuth {
		t.Fatalf("upstream Authorization = %q, want %q", gotAuth, wantAuth)
	}
	if gotPath != "/owner/repo.git/info/refs" {
		t.Fatalf("upstream path = %q, want /owner/repo.git/info/refs", gotPath)
	}
	if gotProtocol != "version=2" {
		t.Fatalf("Git-Protocol not forwarded, got %q", gotProtocol)
	}
}

func TestRelayInvalidOwnerCharsReturns404(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("upstream should not be reached for invalid owner")
	}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/ow%2Fner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}

func TestRelayInfoRefsInvalidServiceReturns400(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("upstream should not be reached for invalid service")
	}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=something-else", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestRelayUnknownRouteUnder404(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/something-unsupported", nil)
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}

func TestRelayForeignHostRedirectBlocked(t *testing.T) {
	evil := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("evil host should never be reached by the test itself")
	}))
	defer evil.Close()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", evil.URL+"/owner/repo.git/info/refs?token=leak")
		w.WriteHeader(http.StatusFound)
	}))
	defer upstream.Close()
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 for cross-host redirect", rec.Code)
	}
}

func TestRelaySameHostRedirectPassesWithCredentialsStripped(t *testing.T) {
	var upstreamURL string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		loc := upstreamURL + "/owner/repo.git/info/refs?service=git-upload-pack"
		w.Header().Set("Location", "http://user:secret@"+strings.TrimPrefix(loc, "http://"))
		w.WriteHeader(http.StatusFound)
	}))
	defer upstream.Close()
	upstreamURL = upstream.URL
	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusFound {
		t.Fatalf("status = %d, want 302 passthrough for same-host redirect", rec.Code)
	}
	loc := rec.Header().Get("Location")
	if strings.Contains(loc, "secret") || strings.Contains(loc, "user@") {
		t.Fatalf("Location leaks credentials: %q", loc)
	}
}

func TestAuditOutputContainsNoTokenOrJWTFragments(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	}))
	defer upstream.Close()

	var buf strings.Builder
	tokens := testTokenSource(t, upstream)
	h, err := NewHandler("correct-token", tokens, upstream.URL, audit.NewLogger(&buf))
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)

	logOutput := buf.String()
	if strings.Contains(logOutput, "correct-token") || strings.Contains(logOutput, "install-token-abc") {
		t.Fatalf("audit log leaks sensitive material: %s", logOutput)
	}
}
