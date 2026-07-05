package jquants

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestIDTokenMintAndCache(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", r.Method)
		}
		if r.URL.Path != "/v1/token/auth_refresh" {
			t.Errorf("path = %s", r.URL.Path)
		}
		token := r.URL.Query().Get("refreshtoken")
		if token != "fake-refresh-token" {
			t.Errorf("refreshtoken = %s", token)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"idToken": "fake-id-token"})
	}))
	defer server.Close()

	ts := NewTokenSource("fake-refresh-token", server.URL)
	id, err := ts.IDToken()
	if err != nil {
		t.Fatalf("IDToken() error = %v", err)
	}
	if id != "fake-id-token" {
		t.Errorf("idToken = %s", id)
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want 1", calls)
	}

	// Second call within TTL must be served from cache (no new upstream call).
	id2, err := ts.IDToken()
	if err != nil {
		t.Fatalf("IDToken() second call error = %v", err)
	}
	if id2 != "fake-id-token" {
		t.Errorf("cached idToken = %s", id2)
	}
	if calls != 1 {
		t.Fatalf("calls after cached call = %d, want still 1", calls)
	}
}

func TestIDTokenRefreshTokenIsURLEscaped(t *testing.T) {
	var seenRawQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenRawQuery = r.URL.RawQuery
		_ = json.NewEncoder(w).Encode(map[string]string{"idToken": "tok"})
	}))
	defer server.Close()

	raw := "a b/c+d"
	ts := NewTokenSource(raw, server.URL)
	if _, err := ts.IDToken(); err != nil {
		t.Fatalf("IDToken() error = %v", err)
	}
	values, err := url.ParseQuery(seenRawQuery)
	if err != nil {
		t.Fatalf("ParseQuery: %v", err)
	}
	if values.Get("refreshtoken") != raw {
		t.Errorf("decoded refreshtoken = %q, want %q", values.Get("refreshtoken"), raw)
	}
}

func TestIDTokenUpstreamErrorDoesNotLeakToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte("invalid refresh token"))
	}))
	defer server.Close()

	ts := NewTokenSource("super-secret-refresh-token", server.URL)
	_, err := ts.IDToken()
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if strings.Contains(err.Error(), "super-secret-refresh-token") {
		t.Fatalf("error leaked refresh token: %v", err)
	}
}

func TestNewTokenSourceFromEnvRequiresRefreshToken(t *testing.T) {
	t.Setenv("JQUANTS_REFRESH_TOKEN", "")
	if _, err := NewTokenSourceFromEnv(); err == nil {
		t.Fatal("expected error when JQUANTS_REFRESH_TOKEN is unset")
	}
}

func TestNewTokenSourceFromEnvDefaultsAPIBase(t *testing.T) {
	t.Setenv("JQUANTS_REFRESH_TOKEN", "tok")
	t.Setenv("JQUANTS_API_BASE_URL", "")
	ts, err := NewTokenSourceFromEnv()
	if err != nil {
		t.Fatalf("NewTokenSourceFromEnv() error = %v", err)
	}
	if ts.APIBase() != defaultAPIBase {
		t.Errorf("APIBase() = %s, want %s", ts.APIBase(), defaultAPIBase)
	}
}
