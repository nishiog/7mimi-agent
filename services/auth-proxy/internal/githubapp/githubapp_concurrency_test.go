package githubapp

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestConcurrentTokenCallsMintOnceWhenCacheFresh drives many goroutines
// through Token() concurrently once a fresh token is cached, to catch data
// races on the shared cache fields (run with -race) and confirm callers
// never see a mint once the cache is warm and far from expiry.
func TestConcurrentTokenCallsMintOnceWhenCacheFresh(t *testing.T) {
	key := generateTestKey(t)
	var mintCount int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/app/installations":
			w.Write([]byte(`[{"id":1}]`))
		case strings.Contains(r.URL.Path, "/access_tokens"):
			n := atomic.AddInt64(&mintCount, 1)
			resp := `{"token":"tok-` + strconv.FormatInt(n, 10) + `","expires_at":"` +
				time.Now().Add(1*time.Hour).UTC().Format(time.RFC3339) + `"}`
			w.Write([]byte(resp))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	ts := NewTokenSource("1", "", key, server.URL)

	// Warm the cache once, sequentially, before hammering it concurrently.
	if _, err := ts.Token(newTestContext()); err != nil {
		t.Fatalf("warming cache: %v", err)
	}
	warmed := atomic.LoadInt64(&mintCount)

	const n = 50
	var wg sync.WaitGroup
	wg.Add(n)
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			if _, err := ts.Token(newTestContext()); err != nil {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Errorf("Token() concurrent call failed: %v", err)
	}

	if got := atomic.LoadInt64(&mintCount); got != warmed {
		t.Fatalf("mintCount changed from %d to %d; concurrent calls against a fresh cache should not re-mint", warmed, got)
	}
}
