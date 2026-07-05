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

const (
	testBotToken  = "test-bot-token-value"
	testChannelID = "C0123456789"
)

func newTestHandler(t *testing.T, slackAPI *httptest.Server) *Handler {
	t.Helper()
	h, err := NewHandler("test-session-token", testBotToken, testChannelID, slackAPI.URL, audit.NewLogger(&strings.Builder{}))
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

// okSlackAPIServer returns a stub Slack Web API server that asserts the
// Authorization bearer and channel/text fields, records each chunk, and
// replies {"ok":true}.
func okSlackAPIServer(t *testing.T, received *[]string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/chat.postMessage" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer "+testBotToken {
			t.Fatalf("unexpected Authorization header: %q", got)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Fatalf("unexpected Content-Type: %q", ct)
		}
		var payload struct {
			Channel string `json:"channel"`
			Text    string `json:"text"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		if payload.Channel != testChannelID {
			t.Fatalf("unexpected channel: %q", payload.Channel)
		}
		if received != nil {
			*received = append(*received, payload.Text)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	}))
}

func TestNewHandlerRequiresAllFields(t *testing.T) {
	logger := audit.NewLogger(&strings.Builder{})
	cases := []struct {
		name         string
		sessionToken string
		botToken     string
		channelID    string
	}{
		{"missing session token", "", testBotToken, testChannelID},
		{"missing bot token", "test-session-token", "", testChannelID},
		{"missing channel id", "test-session-token", testBotToken, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := NewHandler(tc.sessionToken, tc.botToken, tc.channelID, "", logger); err == nil {
				t.Fatalf("expected error, got nil")
			}
		})
	}
}

func TestAuthMissingToken(t *testing.T) {
	slackAPI := okSlackAPIServer(t, nil)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

	rec := doNotify(t, h, "", `{"text":"hi"}`)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

func TestAuthWrongToken(t *testing.T) {
	slackAPI := okSlackAPIServer(t, nil)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

	rec := doNotify(t, h, "wrong-token", `{"text":"hi"}`)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

func TestEmptyTextRejected(t *testing.T) {
	slackAPI := okSlackAPIServer(t, nil)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

	for _, body := range []string{`{"text":""}`, `{}`, ``} {
		rec := doNotify(t, h, "test-session-token", body)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %q: expected 400, got %d", body, rec.Code)
		}
	}
}

func TestOversizePayloadRejected(t *testing.T) {
	slackAPI := okSlackAPIServer(t, nil)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

	big := strings.Repeat("a", 200*1024+100)
	body, _ := json.Marshal(map[string]string{"text": big})
	rec := doNotify(t, h, "test-session-token", string(body))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d", rec.Code)
	}
}

func TestSingleChunkPassthrough(t *testing.T) {
	var received []string
	slackAPI := okSlackAPIServer(t, &received)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

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
		t.Fatalf("unexpected slack api payloads: %v", received)
	}
}

func TestMultiChunkLineBoundarySplit(t *testing.T) {
	var received []string
	slackAPI := okSlackAPIServer(t, &received)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

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
	slackAPI := okSlackAPIServer(t, &received)
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

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

func TestSlackAPIHTTPErrorReturns502NoTokenLeak(t *testing.T) {
	slackAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

	rec := doNotify(t, h, "test-session-token", `{"text":"hi"}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), testBotToken) {
		t.Fatalf("response leaked bot token: %s", rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "hi") {
		t.Fatalf("response leaked message text: %s", rec.Body.String())
	}
}

func TestSlackAPIOKFalseReturns502WithErrorCodeNoTokenLeak(t *testing.T) {
	slackAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": false, "error": "channel_not_found"})
	}))
	defer slackAPI.Close()
	h := newTestHandler(t, slackAPI)

	rec := doNotify(t, h, "test-session-token", `{"text":"hi"}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "channel_not_found") {
		t.Fatalf("expected error code in response, got: %s", rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), testBotToken) {
		t.Fatalf("response leaked bot token: %s", rec.Body.String())
	}
}

func TestAuditLogsNoTextOrToken(t *testing.T) {
	var logBuf strings.Builder
	slackAPI := okSlackAPIServer(t, nil)
	defer slackAPI.Close()
	h, err := NewHandler("test-session-token", testBotToken, testChannelID, slackAPI.URL, audit.NewLogger(&logBuf))
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	h.sleep = func(time.Duration) {}

	secretText := "super-secret-message-content token=" + testBotToken
	rec := doNotify(t, h, "test-session-token", mustJSON(t, secretText))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	logOutput := logBuf.String()
	if strings.Contains(logOutput, "super-secret-message-content") {
		t.Fatalf("audit log leaked message content: %s", logOutput)
	}
	if strings.Contains(logOutput, testBotToken) {
		t.Fatalf("audit log leaked bot token: %s", logOutput)
	}
	if strings.Contains(logOutput, testChannelID) {
		t.Fatalf("audit log leaked channel id: %s", logOutput)
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
