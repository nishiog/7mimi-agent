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
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

func newTestContext() context.Context {
	return context.Background()
}

func generateTestKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating test key: %v", err)
	}
	return key
}

func decodeJWTSegment(t *testing.T, segment string) map[string]any {
	t.Helper()
	raw, err := base64.RawURLEncoding.DecodeString(segment)
	if err != nil {
		t.Fatalf("decoding jwt segment: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshalling jwt segment: %v", err)
	}
	return out
}

func TestAppJWTHasThreeSegmentsAndVerifiesRS256(t *testing.T) {
	key := generateTestKey(t)
	ts := NewTokenSource("12345", "", key, "")

	jwt, err := ts.appJWT()
	if err != nil {
		t.Fatalf("appJWT: %v", err)
	}

	segments := strings.Split(jwt, ".")
	if len(segments) != 3 {
		t.Fatalf("expected 3 segments, got %d", len(segments))
	}

	header := decodeJWTSegment(t, segments[0])
	if header["alg"] != "RS256" || header["typ"] != "JWT" {
		t.Fatalf("unexpected header: %v", header)
	}

	claims := decodeJWTSegment(t, segments[1])
	if claims["iss"] != "12345" {
		t.Fatalf("unexpected iss claim: %v", claims)
	}

	signingInput := segments[0] + "." + segments[1]
	sig, err := base64.RawURLEncoding.DecodeString(segments[2])
	if err != nil {
		t.Fatalf("decoding signature: %v", err)
	}

	digest := sha256.Sum256([]byte(signingInput))
	if err := rsa.VerifyPKCS1v15(&key.PublicKey, crypto.SHA256, digest[:], sig); err != nil {
		t.Fatalf("signature does not verify: %v", err)
	}
}

func TestInstallationAutoDiscoveryOneInstallation(t *testing.T) {
	key := generateTestKey(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/app/installations" {
			w.Write([]byte(`[{"id":999}]`))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	ts := NewTokenSource("1", "", key, server.URL)
	id, err := ts.installationIDFor(newTestContext())
	if err != nil {
		t.Fatalf("installationIDFor: %v", err)
	}
	if id != "999" {
		t.Fatalf("id = %q, want 999", id)
	}
}

func TestInstallationAutoDiscoveryMultipleInstallationsErrors(t *testing.T) {
	key := generateTestKey(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[{"id":1},{"id":2}]`))
	}))
	defer server.Close()

	ts := NewTokenSource("1", "", key, server.URL)
	_, err := ts.installationIDFor(newTestContext())
	if err == nil {
		t.Fatal("expected error for multiple installations, got nil")
	}
}

func TestTokenCachesAndRefreshesNearExpiry(t *testing.T) {
	key := generateTestKey(t)
	var mintCount int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/app/installations":
			w.Write([]byte(`[{"id":1}]`))
		case strings.Contains(r.URL.Path, "/access_tokens"):
			mintCount++
			var expiresAt time.Time
			if mintCount == 1 {
				// near-expiry so a second call must refresh
				expiresAt = time.Now().Add(1 * time.Minute)
			} else {
				expiresAt = time.Now().Add(1 * time.Hour)
			}
			resp := map[string]string{
				"token":      "tok-" + strconv.Itoa(mintCount),
				"expires_at": expiresAt.UTC().Format(time.RFC3339),
			}
			body, _ := json.Marshal(resp)
			w.Write(body)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	ts := NewTokenSource("1", "", key, server.URL)

	tok1, err := ts.Token(newTestContext())
	if err != nil {
		t.Fatalf("Token (1st call): %v", err)
	}
	if tok1 != "tok-1" {
		t.Fatalf("tok1 = %q, want tok-1", tok1)
	}

	tok2, err := ts.Token(newTestContext())
	if err != nil {
		t.Fatalf("Token (2nd call): %v", err)
	}
	if tok2 != "tok-2" {
		t.Fatalf("tok2 = %q (mintCount=%d), want tok-2 (cache should refresh near expiry)", tok2, mintCount)
	}

	if mintCount != 2 {
		t.Fatalf("mintCount = %d, want 2", mintCount)
	}
}

func TestNewTokenSourceFromEnvErrorsOnMissingFields(t *testing.T) {
	t.Setenv("GITHUB_APP_ID", "")
	t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", "")
	if _, err := NewTokenSourceFromEnv(); err == nil {
		t.Fatal("expected error when GITHUB_APP_ID and key path are unset")
	}
}

func TestErrorsNeverContainKeyOrTokenMaterial(t *testing.T) {
	key := generateTestKey(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()

	ts := NewTokenSource("1", "", key, server.URL)
	_, err := ts.Token(newTestContext())
	if err == nil {
		t.Fatal("expected error")
	}
	// Must not leak PEM markers or raw key bytes.
	keyBytes := x509.MarshalPKCS1PrivateKey(key)
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: keyBytes})
	if strings.Contains(err.Error(), string(pemBytes)) {
		t.Fatalf("error leaks private key material: %v", err)
	}
}
