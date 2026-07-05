package jqmcp

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/jquants"
)

func newFakeJQuantsServer(t *testing.T, wantPath string, respBody string, respStatus int) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/token/auth_refresh" {
			_, _ = w.Write([]byte(`{"idToken":"fake-id-token"}`))
			return
		}
		if wantPath != "" && r.URL.Path != wantPath {
			t.Errorf("path = %s, want %s", r.URL.Path, wantPath)
		}
		w.WriteHeader(respStatus)
		_, _ = w.Write([]byte(respBody))
	}))
	return server
}

func TestGetListedInfoHappyPath(t *testing.T) {
	server := newFakeJQuantsServer(t, "/v1/listed/info", `{"info":[{"Code":"7203"}]}`, http.StatusOK)
	defer server.Close()

	tokens := jquants.NewTokenSource("refresh-tok", server.URL)
	handler := handleListedInfo(tokens)
	result := handler(map[string]any{"code": "7203"})

	if result.IsError() {
		t.Fatalf("unexpected error result: %s", result.Text())
	}
	if !strings.Contains(result.Text(), `"Code":"7203"`) {
		t.Errorf("text = %s", result.Text())
	}
}

func TestGetListedInfoSendsBearerAndQuery(t *testing.T) {
	var gotAuth, gotQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/token/auth_refresh" {
			_, _ = w.Write([]byte(`{"idToken":"fake-id-token"}`))
			return
		}
		gotAuth = r.Header.Get("Authorization")
		gotQuery = r.URL.Query().Get("code")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	tokens := jquants.NewTokenSource("refresh-tok", server.URL)
	handler := handleListedInfo(tokens)
	handler(map[string]any{"code": "7203"})

	if gotAuth != "Bearer fake-id-token" {
		t.Errorf("Authorization header = %q", gotAuth)
	}
	if gotQuery != "7203" {
		t.Errorf("code query = %q", gotQuery)
	}
}

func TestGetDailyQuotesSendsFromTo(t *testing.T) {
	var gotQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/token/auth_refresh" {
			_, _ = w.Write([]byte(`{"idToken":"fake-id-token"}`))
			return
		}
		gotQuery = r.URL.RawQuery
		_, _ = w.Write([]byte(`{"daily_quotes":[]}`))
	}))
	defer server.Close()

	tokens := jquants.NewTokenSource("refresh-tok", server.URL)
	handler := handleDailyQuotes(tokens)
	result := handler(map[string]any{"code": "7203", "from": "2024-01-01", "to": "2024-01-31"})

	if result.IsError() {
		t.Fatalf("unexpected error: %s", result.Text())
	}
	if !strings.Contains(gotQuery, "from=2024-01-01") || !strings.Contains(gotQuery, "to=2024-01-31") {
		t.Errorf("query = %s", gotQuery)
	}
}

func TestGetStatementsHappyPath(t *testing.T) {
	server := newFakeJQuantsServer(t, "/v1/fins/statements", `{"statements":[]}`, http.StatusOK)
	defer server.Close()

	tokens := jquants.NewTokenSource("refresh-tok", server.URL)
	handler := handleStatements(tokens)
	result := handler(map[string]any{"code": "7203"})
	if result.IsError() {
		t.Fatalf("unexpected error: %s", result.Text())
	}
}

func TestMissingCodeArgumentIsError(t *testing.T) {
	tokens := jquants.NewTokenSource("refresh-tok", "http://unused.invalid")
	result := handleListedInfo(tokens)(map[string]any{})
	if !result.IsError() {
		t.Fatal("expected error result for missing code")
	}
}

func TestUpstreamErrorNeverLeaksToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/token/auth_refresh" {
			_, _ = w.Write([]byte(`{"idToken":"super-secret-id-token"}`))
			return
		}
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("forbidden"))
	}))
	defer server.Close()

	tokens := jquants.NewTokenSource("refresh-tok", server.URL)
	result := handleListedInfo(tokens)(map[string]any{"code": "7203"})
	if !result.IsError() {
		t.Fatal("expected error result")
	}
	if strings.Contains(result.Text(), "super-secret-id-token") {
		t.Fatalf("error text leaked idToken: %s", result.Text())
	}
	if strings.Contains(result.Text(), "refresh-tok") {
		t.Fatalf("error text leaked refresh token: %s", result.Text())
	}
}

func TestUpstreamNonJSONResponseIsError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/token/auth_refresh" {
			_, _ = w.Write([]byte(`{"idToken":"fake-id-token"}`))
			return
		}
		_, _ = w.Write([]byte("not json"))
	}))
	defer server.Close()

	tokens := jquants.NewTokenSource("refresh-tok", server.URL)
	result := handleListedInfo(tokens)(map[string]any{"code": "7203"})
	if !result.IsError() {
		t.Fatal("expected error result for non-JSON upstream response")
	}
}
