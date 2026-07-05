// Package jquants mints and caches J-Quants API idToken values (ADR-027).
// It never logs the refresh token, the idToken, or request/response bodies
// containing them.
package jquants

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const defaultAPIBase = "https://api.jquants.com"

// idTokenTTL is the assumed lifetime of a minted idToken. J-Quants does not
// return an explicit expiry in the auth_refresh response, so a conservative
// ~24h assumption (refreshed well before expiry) is used, matching the ADR.
const idTokenTTL = 23 * time.Hour

// refreshMargin: mint a new idToken once the cached one has less than this
// much time remaining.
const refreshMargin = 30 * time.Minute

// TokenSource mints and caches J-Quants idToken values from a refresh token.
type TokenSource struct {
	refreshToken string
	apiBase      string
	httpClient   *http.Client

	mu          sync.Mutex
	cachedToken string
	expiresAt   time.Time
}

// NewTokenSourceFromEnv builds a TokenSource from JQUANTS_REFRESH_TOKEN and
// optional JQUANTS_API_BASE_URL. Returns an error (never containing the
// token) when JQUANTS_REFRESH_TOKEN is not set.
func NewTokenSourceFromEnv() (*TokenSource, error) {
	refreshToken := os.Getenv("JQUANTS_REFRESH_TOKEN")
	if refreshToken == "" {
		return nil, errors.New("JQUANTS_REFRESH_TOKEN is not set")
	}
	apiBase := os.Getenv("JQUANTS_API_BASE_URL")
	return NewTokenSource(refreshToken, apiBase), nil
}

// NewTokenSource builds a TokenSource directly, useful for tests.
func NewTokenSource(refreshToken, apiBase string) *TokenSource {
	if apiBase == "" {
		apiBase = defaultAPIBase
	}
	return &TokenSource{
		refreshToken: refreshToken,
		apiBase:      strings.TrimRight(apiBase, "/"),
		httpClient:   &http.Client{Timeout: 20 * time.Second},
	}
}

type idTokenResponse struct {
	IDToken string `json:"idToken"`
}

// IDToken returns a cached idToken, minting a new one if the cache is empty
// or within refreshMargin of assumed expiry.
func (t *TokenSource) IDToken() (string, error) {
	t.mu.Lock()
	if t.cachedToken != "" && time.Until(t.expiresAt) > refreshMargin {
		token := t.cachedToken
		t.mu.Unlock()
		return token, nil
	}
	t.mu.Unlock()

	endpoint := t.apiBase + "/v1/token/auth_refresh?refreshtoken=" + url.QueryEscape(t.refreshToken)
	req, err := http.NewRequest(http.MethodPost, endpoint, nil)
	if err != nil {
		return "", errors.New("failed to build auth_refresh request")
	}

	resp, err := t.httpClient.Do(req)
	if err != nil {
		return "", errors.New("failed to reach jquants auth_refresh endpoint")
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", errors.New("failed to read jquants auth_refresh response")
	}

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("jquants auth_refresh: unexpected status %d", resp.StatusCode)
	}

	var parsed idTokenResponse
	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", errors.New("failed to decode jquants auth_refresh response")
	}
	if parsed.IDToken == "" {
		return "", errors.New("jquants auth_refresh response missing idToken")
	}

	t.mu.Lock()
	t.cachedToken = parsed.IDToken
	t.expiresAt = time.Now().Add(idTokenTTL)
	t.mu.Unlock()

	return parsed.IDToken, nil
}

// APIBase returns the configured (or default) J-Quants API base URL.
func (t *TokenSource) APIBase() string {
	return t.apiBase
}

// HTTPClient returns the shared HTTP client used for API calls.
func (t *TokenSource) HTTPClient() *http.Client {
	return t.httpClient
}
