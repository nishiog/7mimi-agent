package slacknotify

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
)

func newTestHandler(t *testing.T, webhook *httptest.Server) *Handler {
	t.Helper()
	h, err := NewHandler("test-session-token", webhook.URL, audit.NewLogger(&strings.Builder{}))
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	h.sleep = func(time.Duration) {} // no real sleeping in tests
	return h
}

func doNotify(t *testing.T, h *Handler, token string, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/slack/notify", strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.Routes().ServeHTTP(rec, req)
	return rec
}

func TestAuthMissingToken(t *testing.T) {
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	rec := doNotify(t, h, "", `{"text":"hi"}`)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

func TestAuthWrongToken(t *testing.T) {
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	rec := doNotify(t, h, "wrong-token", `{"text":"hi"}`)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

func TestEmptyTextRejected(t *testing.T) {
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	for _, body := range []string{`{"text":""}`, `{}`, ``} {
		rec := doNotify(t, h, "test-session-token", body)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %q: expected 400, got %d", body, rec.Code)
		}
	}
}

func TestOversizePayloadRejected(t *testing.T) {
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	big := strings.Repeat("a", 200*1024+100)
	body, _ := json.Marshal(map[string]string{"text": big})
	rec := doNotify(t, h, "test-session-token", string(body))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d", rec.Code)
	}
}

func TestSingleChunkPassthrough(t *testing.T) {
	var received []string
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]string
		_ = json.NewDecoder(r.Body).Decode(&payload)
		received = append(received, payload["text"])
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	rec := doNotify(t, h, "test-session-token", `{"text":"hello slack"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var resp notifyResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Chunks != 1 {
		t.Fatalf("expected 1 chunk, got %d", resp.Chunks)
	}
	if len(received) != 1 || received[0] != "hello slack" {
		t.Fatalf("unexpected webhook payloads: %v", received)
	}
}

func TestMultiChunkLineBoundarySplit(t *testing.T) {
	var received []string
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]string
		_ = json.NewDecoder(r.Body).Decode(&payload)
		received = append(received, payload["text"])
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	// Build text with distinct lines that must total > 3500 chars so it
	// splits into multiple chunks, verifying no line is cut mid-line.
	lines := make([]string, 0, 100)
	for i := 0; i < 100; i++ {
		lines = append(lines, strings.Repeat("x", 80)+"-line-marker-end")
	}
	text := strings.Join(lines, "\n")

	rec := doNotify(t, h, "test-session-token", mustJSON(t, text))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(received) < 2 {
		t.Fatalf("expected multiple chunks, got %d", len(received))
	}
	for _, chunk := range received {
		if len([]rune(chunk)) > maxChunkLen {
			t.Fatalf("chunk exceeds maxChunkLen: %d", len(chunk))
		}
		// Every full line must appear intact — no line is cut mid-line.
		for _, line := range strings.Split(chunk, "\n") {
			if line == "" {
				continue
			}
			if !strings.HasSuffix(line, "-line-marker-end") && len([]rune(line)) != 3500 {
				// A line that's neither a whole marked line nor a full hard-split
				// chunk of maxChunkLen chars would indicate a mid-line cut.
				t.Fatalf("suspicious partial line found: %q", line)
			}
		}
	}
	// Reassembling all chunks (joined by the same separator used internally)
	// should reproduce every original line somewhere across the chunks.
	joinedBack := strings.Join(received, "\n")
	for _, line := range lines {
		if !strings.Contains(joinedBack, line) {
			t.Fatalf("original line missing from reassembled output: %q", line)
		}
	}
}

func TestLongSingleLineHardSplit(t *testing.T) {
	var received []string
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]string
		_ = json.NewDecoder(r.Body).Decode(&payload)
		received = append(received, payload["text"])
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	longLine := strings.Repeat("a", 9000)
	rec := doNotify(t, h, "test-session-token", mustJSON(t, longLine))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(received) < 3 {
		t.Fatalf("expected the 9000-char line to hard-split into >=3 chunks, got %d", len(received))
	}
	reassembled := strings.Join(received, "")
	if reassembled != longLine {
		t.Fatalf("hard-split chunks do not reassemble to the original line")
	}
	for _, chunk := range received {
		if len([]rune(chunk)) > maxChunkLen {
			t.Fatalf("chunk exceeds maxChunkLen: %d", len(chunk))
		}
	}
}

func TestWebhookErrorReturns502NoURLLeak(t *testing.T) {
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer webhook.Close()
	h := newTestHandler(t, webhook)

	rec := doNotify(t, h, "test-session-token", `{"text":"hi"}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), webhook.URL) {
		t.Fatalf("response leaked webhook URL: %s", rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "hi") {
		t.Fatalf("response leaked message text: %s", rec.Body.String())
	}
}

func TestAuditLogsNoTextOrURL(t *testing.T) {
	webhook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer webhook.Close()

	var logBuf strings.Builder
	h, err := NewHandler("test-session-token", webhook.URL, audit.NewLogger(&logBuf))
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	h.sleep = func(time.Duration) {}

	secretText := "super-secret-message-content webhook=" + webhook.URL
	rec := doNotify(t, h, "test-session-token", mustJSON(t, secretText))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	logOutput := logBuf.String()
	if strings.Contains(logOutput, "super-secret-message-content") {
		t.Fatalf("audit log leaked message content: %s", logOutput)
	}
	if strings.Contains(logOutput, webhook.URL) {
		t.Fatalf("audit log leaked webhook URL: %s", logOutput)
	}
	if !strings.Contains(logOutput, "chunks=") || !strings.Contains(logOutput, "total_len=") || !strings.Contains(logOutput, "duration_ms=") {
		t.Fatalf("audit log missing expected metadata fields: %s", logOutput)
	}
}

func mustJSON(t *testing.T, text string) string {
	t.Helper()
	b, err := json.Marshal(map[string]string{"text": text})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}
