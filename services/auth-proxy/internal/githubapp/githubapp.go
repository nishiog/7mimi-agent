// Package githubapp mints short-lived GitHub App installation access tokens
// for the git relay (ADR-020). It never logs the App private key, the JWT it
// signs, or the installation tokens it mints.
package githubapp

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"
)

const defaultAPIBase = "https://api.github.com"

// TokenSource mints and caches GitHub App installation access tokens.
type TokenSource struct {
	appID          string
	installationID string // empty means auto-discover
	privateKey     *rsa.PrivateKey
	apiBase        string
	httpClient     *http.Client

	mu          sync.Mutex
	cachedToken string
	expiresAt   time.Time
	resolvedID  string
}

// NewTokenSourceFromEnv builds a TokenSource from GITHUB_APP_ID,
// GITHUB_APP_INSTALLATION_ID (optional), and GITHUB_APP_PRIVATE_KEY_PATH.
func NewTokenSourceFromEnv() (*TokenSource, error) {
	appID := os.Getenv("GITHUB_APP_ID")
	installationID := os.Getenv("GITHUB_APP_INSTALLATION_ID")
	keyPath := os.Getenv("GITHUB_APP_PRIVATE_KEY_PATH")

	if appID == "" {
		return nil, errors.New("GITHUB_APP_ID is not set")
	}
	if keyPath == "" {
		return nil, errors.New("GITHUB_APP_PRIVATE_KEY_PATH is not set")
	}

	keyBytes, err := os.ReadFile(keyPath)
	if err != nil {
		return nil, fmt.Errorf("reading private key file: %w", err)
	}

	key, err := parsePrivateKey(keyBytes)
	if err != nil {
		return nil, fmt.Errorf("parsing private key: %w", err)
	}

	apiBase := os.Getenv("GITHUB_API_BASE_URL")
	if apiBase == "" {
		apiBase = defaultAPIBase
	}

	return NewTokenSource(appID, installationID, key, apiBase), nil
}

// NewTokenSource builds a TokenSource directly, useful for tests.
func NewTokenSource(appID, installationID string, key *rsa.PrivateKey, apiBase string) *TokenSource {
	if apiBase == "" {
		apiBase = defaultAPIBase
	}
	return &TokenSource{
		appID:          appID,
		installationID: installationID,
		privateKey:     key,
		apiBase:        apiBase,
		httpClient:     &http.Client{Timeout: 30 * time.Second},
	}
}

func parsePrivateKey(pemBytes []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, errors.New("no PEM block found")
	}
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("not a PKCS1 or PKCS8 RSA key: %w", err)
	}
	rsaKey, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("private key is not RSA")
	}
	return rsaKey, nil
}

func base64URLEncode(data []byte) string {
	return base64.RawURLEncoding.EncodeToString(data)
}

// appJWT mints a short-lived RS256 App JWT per GitHub App auth requirements.
func (t *TokenSource) appJWT() (string, error) {
	now := time.Now()
	header := map[string]string{"alg": "RS256", "typ": "JWT"}
	claims := map[string]any{
		"iat": now.Add(-60 * time.Second).Unix(),
		"exp": now.Add(540 * time.Second).Unix(),
		"iss": t.appID,
	}

	headerJSON, err := json.Marshal(header)
	if err != nil {
		return "", err
	}
	claimsJSON, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}

	signingInput := base64URLEncode(headerJSON) + "." + base64URLEncode(claimsJSON)
	digest := sha256.Sum256([]byte(signingInput))
	signature, err := rsa.SignPKCS1v15(rand.Reader, t.privateKey, crypto.SHA256, digest[:])
	if err != nil {
		return "", errors.New("failed to sign app jwt")
	}

	return signingInput + "." + base64URLEncode(signature), nil
}

type installation struct {
	ID int64 `json:"id"`
}

// installationIDFor resolves the installation id to use: the configured one,
// or auto-discovered when exactly one installation exists.
func (t *TokenSource) installationIDFor(ctx context.Context) (string, error) {
	if t.installationID != "" {
		return t.installationID, nil
	}

	t.mu.Lock()
	resolved := t.resolvedID
	t.mu.Unlock()
	if resolved != "" {
		return resolved, nil
	}

	jwt, err := t.appJWT()
	if err != nil {
		return "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, t.apiBase+"/app/installations", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+jwt)
	req.Header.Set("Accept", "application/vnd.github+json")

	resp, err := t.httpClient.Do(req)
	if err != nil {
		return "", errors.New("failed to list app installations")
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("listing app installations: unexpected status %d", resp.StatusCode)
	}

	var installations []installation
	if err := json.NewDecoder(resp.Body).Decode(&installations); err != nil {
		return "", errors.New("failed to decode app installations response")
	}

	if len(installations) != 1 {
		return "", fmt.Errorf("expected exactly 1 app installation, found %d", len(installations))
	}

	id := strconv.FormatInt(installations[0].ID, 10)
	t.mu.Lock()
	t.resolvedID = id
	t.mu.Unlock()
	return id, nil
}

type accessTokenResponse struct {
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

// Token returns a cached installation access token, minting a new one if the
// cache is empty or within 5 minutes of expiry.
func (t *TokenSource) Token(ctx context.Context) (string, error) {
	t.mu.Lock()
	if t.cachedToken != "" && time.Until(t.expiresAt) > 5*time.Minute {
		token := t.cachedToken
		t.mu.Unlock()
		return token, nil
	}
	t.mu.Unlock()

	installationID, err := t.installationIDFor(ctx)
	if err != nil {
		return "", err
	}

	jwt, err := t.appJWT()
	if err != nil {
		return "", err
	}

	url := t.apiBase + "/app/installations/" + installationID + "/access_tokens"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+jwt)
	req.Header.Set("Accept", "application/vnd.github+json")

	resp, err := t.httpClient.Do(req)
	if err != nil {
		return "", errors.New("failed to mint installation access token")
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, resp.Body)
		return "", fmt.Errorf("minting installation access token: unexpected status %d", resp.StatusCode)
	}

	var body accessTokenResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", errors.New("failed to decode installation access token response")
	}
	if body.Token == "" {
		return "", errors.New("installation access token response missing token")
	}

	expiresAt, err := time.Parse(time.RFC3339, body.ExpiresAt)
	if err != nil {
		return "", errors.New("failed to parse installation access token expiry")
	}

	t.mu.Lock()
	t.cachedToken = body.Token
	t.expiresAt = expiresAt
	t.mu.Unlock()

	return body.Token, nil
}
