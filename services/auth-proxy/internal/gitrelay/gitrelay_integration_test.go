package gitrelay

import (
	"bytes"
	"compress/gzip"
	"crypto/rand"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

// TestRelayPassesGitProtocolAdvertisementBytesUnmodified exercises the relay
// against a fake upstream that returns a realistic smart-HTTP info/refs
// advertisement (pkt-line framed, correct Content-Type) and asserts the
// response body reaches the client byte-for-byte, with headers preserved.
func TestRelayPassesGitProtocolAdvertisementBytesUnmodified(t *testing.T) {
	// Minimal but structurally real git-upload-pack advertisement:
	// pkt-line length-prefixed lines, followed by a flush-pkt.
	line := "# service=git-upload-pack\n"
	pkt := func(s string) string {
		n := len(s) + 4
		return hexLen(n) + s
	}
	body := pkt(line) + "0000" +
		pkt("0000000000000000000000000000000000000000 capabilities^{}\x00multi_ack thin-pack side-band side-band-64k ofs-delta shallow no-progress include-tag\n") +
		"0000"

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/owner/repo.git/info/refs" {
			t.Errorf("unexpected upstream path: %s", r.URL.Path)
		}
		if r.Header.Get("Git-Protocol") != "version=2" {
			t.Errorf("Git-Protocol header not forwarded, got %q", r.Header.Get("Git-Protocol"))
		}
		w.Header().Set("Content-Type", "application/x-git-upload-pack-advertisement")
		w.Write([]byte(body))
	}))
	defer upstream.Close()

	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
	req.Header.Set("Authorization", "Bearer correct-token")
	req.Header.Set("Git-Protocol", "version=2")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/x-git-upload-pack-advertisement" {
		t.Fatalf("Content-Type = %q, want advertisement type", ct)
	}
	if rec.Body.String() != body {
		t.Fatalf("body mismatch:\n got  %q\n want %q", rec.Body.String(), body)
	}
}

func hexLen(n int) string {
	const hexdigits = "0123456789abcdef"
	b := make([]byte, 4)
	for i := 3; i >= 0; i-- {
		b[i] = hexdigits[n&0xf]
		n >>= 4
	}
	return string(b)
}

// TestRelayPassesLargeBinaryPostBodyIntact verifies a ~1MB binary POST body
// (simulating a git-upload-pack negotiation / receive-pack payload) reaches
// the upstream and the upstream's response reaches the client without
// truncation or corruption.
func TestRelayPassesLargeBinaryPostBodyIntact(t *testing.T) {
	const size = 1 << 20 // 1 MiB
	reqBody := make([]byte, size)
	if _, err := rand.Read(reqBody); err != nil {
		t.Fatalf("generating random body: %v", err)
	}
	respBody := make([]byte, size+37)
	if _, err := rand.Read(respBody); err != nil {
		t.Fatalf("generating random response body: %v", err)
	}

	var gotBody []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var err error
		gotBody, err = io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("reading upstream request body: %v", err)
		}
		w.Header().Set("Content-Type", "application/x-git-upload-pack-result")
		w.Write(respBody)
	}))
	defer upstream.Close()

	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodPost, "/git/owner/repo/git-upload-pack", bytes.NewReader(reqBody))
	req.Header.Set("Authorization", "Bearer correct-token")
	req.Header.Set("Content-Type", "application/x-git-upload-pack-request")
	req.ContentLength = int64(len(reqBody))
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if !bytes.Equal(gotBody, reqBody) {
		t.Fatalf("upstream received body of len %d, want %d, equal=%v", len(gotBody), len(reqBody), bytes.Equal(gotBody, reqBody))
	}
	if !bytes.Equal(rec.Body.Bytes(), respBody) {
		t.Fatalf("client received body of len %d, want %d", rec.Body.Len(), len(respBody))
	}
}

// TestRelayPassesGzipContentEncodingUntouched verifies that when the client
// sends a gzip-compressed POST body (as git does for large pack negotiation
// requests) with Content-Encoding: gzip, the relay forwards the compressed
// bytes as-is rather than transparently decompressing/recompressing them,
// and gzip response bodies from upstream pass through intact too.
func TestRelayPassesGzipContentEncodingUntouched(t *testing.T) {
	var payload bytes.Buffer
	gz := gzip.NewWriter(&payload)
	if _, err := gz.Write([]byte("0032want deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n0000")); err != nil {
		t.Fatalf("gzip write: %v", err)
	}
	if err := gz.Close(); err != nil {
		t.Fatalf("gzip close: %v", err)
	}
	compressed := payload.Bytes()

	var respPayload bytes.Buffer
	gzResp := gzip.NewWriter(&respPayload)
	gzResp.Write([]byte("0008NAK\n0000"))
	gzResp.Close()
	compressedResp := respPayload.Bytes()

	var gotBody []byte
	var gotEncoding string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotEncoding = r.Header.Get("Content-Encoding")
		var err error
		gotBody, err = io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("reading upstream body: %v", err)
		}
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Set("Content-Type", "application/x-git-upload-pack-result")
		w.Write(compressedResp)
	}))
	defer upstream.Close()

	h := newTestHandler(t, "correct-token", upstream)

	req := httptest.NewRequest(http.MethodPost, "/git/owner/repo/git-upload-pack", bytes.NewReader(compressed))
	req.Header.Set("Authorization", "Bearer correct-token")
	req.Header.Set("Content-Type", "application/x-git-upload-pack-request")
	req.Header.Set("Content-Encoding", "gzip")
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if gotEncoding != "gzip" {
		t.Fatalf("upstream Content-Encoding = %q, want gzip", gotEncoding)
	}
	if !bytes.Equal(gotBody, compressed) {
		t.Fatalf("upstream body (compressed bytes) not passed through unmodified: got %d bytes, want %d", len(gotBody), len(compressed))
	}
	if rec.Header().Get("Content-Encoding") != "gzip" {
		t.Fatalf("client Content-Encoding = %q, want gzip (should not be transparently decoded by transport)", rec.Header().Get("Content-Encoding"))
	}
	if !bytes.Equal(rec.Body.Bytes(), compressedResp) {
		t.Fatalf("client did not receive compressed response bytes unmodified")
	}
}

// TestConcurrentProxyRequestsMintTokenSafely drives many concurrent relay
// requests through a single Handler/TokenSource to catch data races in the
// token cache (run with -race) and confirm the mint-then-cache path doesn't
// corrupt shared state under concurrency.
func TestConcurrentProxyRequestsMintTokenSafely(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-git-upload-pack-advertisement")
		w.Write([]byte("ok"))
	}))
	defer upstream.Close()

	h := newTestHandler(t, "correct-token", upstream)

	const n = 25
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			req := httptest.NewRequest(http.MethodGet, "/git/owner/repo/info/refs?service=git-upload-pack", nil)
			req.Header.Set("Authorization", "Bearer correct-token")
			rec := httptest.NewRecorder()
			h.Routes().ServeHTTP(rec, req)
			if rec.Code != http.StatusOK {
				t.Errorf("status = %d, want 200", rec.Code)
			}
		}()
	}
	wg.Wait()
}
