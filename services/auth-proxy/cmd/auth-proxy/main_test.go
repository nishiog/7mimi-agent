package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
)

func generateTestPEMKey(t *testing.T) []byte {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating test key: %v", err)
	}
	der := x509.MarshalPKCS1PrivateKey(key)
	return pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: der})
}

func writeTempKeyFile(t *testing.T, pemBytes []byte) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "app-key.pem")
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatalf("writing temp key file: %v", err)
	}
	return path
}

// TestMountGitRelayDisabledWithoutSessionToken verifies the relay is not
// mounted (mux has no /git/ handler) when AUTH_PROXY_SESSION_TOKEN is unset,
// matching the ADR-020 requirement that the relay is opt-in.
func TestMountGitRelayDisabledWithoutSessionToken(t *testing.T) {
	t.Setenv("AUTH_PROXY_SESSION_TOKEN", "")
	t.Setenv("GITHUB_APP_ID", "")
	t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", "")

	mux := http.NewServeMux()
	mountGitRelay(mux, audit.NewLogger(io.Discard))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 (relay must not be mounted without a session token)", rec.Code)
	}
}

// TestMountGitRelayDisabledWithoutGitHubAppCreds verifies the relay is not
// mounted when the session token is set but GitHub App credentials are
// missing (TokenSource construction fails).
func TestMountGitRelayDisabledWithoutGitHubAppCreds(t *testing.T) {
	t.Setenv("AUTH_PROXY_SESSION_TOKEN", "some-session-token")
	t.Setenv("GITHUB_APP_ID", "")
	t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", "")

	mux := http.NewServeMux()
	mountGitRelay(mux, audit.NewLogger(io.Discard))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer some-session-token")
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 (relay must not be mounted without GitHub App credentials)", rec.Code)
	}
}

// TestMountGitRelayEnabledWithSessionTokenAndCreds verifies the relay is
// mounted (routes respond, even if unauthorized) once both a session token
// and GitHub App credentials are present.
func TestMountGitRelayEnabledWithSessionTokenAndCreds(t *testing.T) {
	key := generateTestPEMKey(t)
	keyPath := writeTempKeyFile(t, key)

	t.Setenv("AUTH_PROXY_SESSION_TOKEN", "some-session-token")
	t.Setenv("GITHUB_APP_ID", "12345")
	t.Setenv("GITHUB_APP_INSTALLATION_ID", "999")
	t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", keyPath)

	mux := http.NewServeMux()
	mountGitRelay(mux, audit.NewLogger(io.Discard))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	// No Authorization header: relay must be mounted and respond 401 (not 404).
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401 (relay should be mounted and reachable, but reject missing auth)", rec.Code)
	}
}
