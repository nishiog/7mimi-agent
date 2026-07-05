// Package slacknotify implements POST /v1/slack/notify (ADR-026): a
// credential-free agent-runner (or the scheduler orchestrator) is authorized
// via a static session bearer token, and auth-proxy relays the message to
// Slack via the Slack Web API (chat.postMessage) using a Slack App bot token
// it alone holds. The bot token is a secret and is never logged or echoed
// back in error responses.
package slacknotify

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
)

const (
	maxBodyBytes       = 200 * 1024 // 200KB
	maxChunkLen        = 3500
	chunkDelay         = 700 * time.Millisecond
	defaultSlackAPIURL = "https://slack.com"
)

// Handler serves POST /v1/slack/notify.
type Handler struct {
	sessionToken string
	botToken     string
	channelID    string
	apiBase      string
	logger       *audit.Logger
	httpClient   *http.Client
	sleep        func(time.Duration)
}

// NewHandler builds the slacknotify handler. sessionToken, botToken, and
// channelID must all be non-empty (fail-closed; there is no default). apiBase
// defaults to https://slack.com when empty (override via SLACK_API_BASE_URL
// for tests).
func NewHandler(sessionToken, botToken, channelID, apiBase string, logger *audit.Logger) (*Handler, error) {
	if sessionToken == "" {
		return nil, errors.New("slacknotify: session token must not be empty")
	}
	if botToken == "" {
		return nil, errors.New("slacknotify: bot token must not be empty")
	}
	if channelID == "" {
		return nil, errors.New("slacknotify: channel id must not be empty")
	}
	if apiBase == "" {
		apiBase = defaultSlackAPIURL
	}
	return &Handler{
		sessionToken: sessionToken,
		botToken:     botToken,
		channelID:    channelID,
		apiBase:      apiBase,
		logger:       logger,
		httpClient:   &http.Client{Timeout: 20 * time.Second},
		sleep:        time.Sleep,
	}, nil
}

// Routes registers the handler's HTTP routes on a mux.
func (h *Handler) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/slack/notify", h.handleNotify)
	return mux
}

func (h *Handler) authorize(r *http.Request) bool {
	const prefix = "Bearer "
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(auth[len(prefix):]), []byte(h.sessionToken)) == 1
}

type notifyRequest struct {
	Text string `json:"text"`
}

type notifyResponse struct {
	Chunks int `json:"chunks"`
}

func (h *Handler) handleNotify(w http.ResponseWriter, r *http.Request) {
	start := time.Now()

	if !h.authorize(r) {
		h.audit("block", "unauthorized", 0, 0, time.Since(start))
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		h.audit("block", "read error", 0, 0, time.Since(start))
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if len(body) > maxBodyBytes {
		h.audit("block", "payload too large", 0, len(body), time.Since(start))
		http.Error(w, "payload too large", http.StatusRequestEntityTooLarge)
		return
	}

	var req notifyRequest
	if len(body) == 0 || json.Unmarshal(body, &req) != nil {
		h.audit("block", "invalid json", 0, len(body), time.Since(start))
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}
	if req.Text == "" {
		h.audit("block", "empty text", 0, 0, time.Since(start))
		http.Error(w, "text must not be empty", http.StatusBadRequest)
		return
	}

	chunks := splitIntoChunks(req.Text, maxChunkLen)

	for i, chunk := range chunks {
		if i > 0 {
			h.sleep(chunkDelay)
		}
		if err := h.postChunk(chunk); err != nil {
			h.audit("block", "slack api error chunk="+strconv.Itoa(i), len(chunks), len(req.Text), time.Since(start))
			http.Error(w, "upstream slack api error at chunk "+strconv.Itoa(i)+": "+err.Error(), http.StatusBadGateway)
			return
		}
	}

	h.audit("allow", "", len(chunks), len(req.Text), time.Since(start))
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(notifyResponse{Chunks: len(chunks)})
}

// slackAPIError carries a Slack error code (e.g. "channel_not_found") as
// returned by chat.postMessage's "error" field, or a synthesized code for
// transport-level failures. It is never a secret, so it is safe to surface
// in the 502 response.
type slackAPIError struct {
	code string
}

func (e *slackAPIError) Error() string {
	return e.code
}

func (h *Handler) postChunk(text string) error {
	payload, err := json.Marshal(map[string]string{
		"channel": h.channelID,
		"text":    text,
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, h.apiBase+"/api/chat.postMessage", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+h.botToken)
	resp, err := h.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &slackAPIError{code: "http_status_" + strconv.Itoa(resp.StatusCode)}
	}

	var apiResp struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := json.Unmarshal(respBody, &apiResp); err != nil {
		return &slackAPIError{code: "invalid_response"}
	}
	if !apiResp.OK {
		code := apiResp.Error
		if code == "" {
			code = "unknown_error"
		}
		return &slackAPIError{code: code}
	}
	return nil
}

// splitIntoChunks splits text into chunks of at most maxLen characters,
// preferring to split on line boundaries ("\n"). A single line longer than
// maxLen is hard-split at maxLen-character boundaries since there is no
// smaller boundary available.
func splitIntoChunks(text string, maxLen int) []string {
	if text == "" {
		return nil
	}
	lines := strings.Split(text, "\n")

	var chunks []string
	var current strings.Builder

	flush := func() {
		if current.Len() > 0 {
			chunks = append(chunks, current.String())
			current.Reset()
		}
	}

	for _, line := range lines {
		candidateLen := current.Len()
		if candidateLen > 0 {
			candidateLen++ // for the joining "\n"
		}
		candidateLen += len([]rune(line))

		if candidateLen > maxLen {
			flush()
			// The line itself may still be longer than maxLen: hard-split it.
			for _, part := range hardSplit(line, maxLen) {
				if len([]rune(part)) == maxLen {
					chunks = append(chunks, part)
				} else {
					current.WriteString(part)
				}
			}
			continue
		}

		if current.Len() > 0 {
			current.WriteString("\n")
		}
		current.WriteString(line)
	}
	flush()

	return chunks
}

// hardSplit splits a single line into maxLen-rune pieces. The last piece may
// be shorter than maxLen and is left in the running "current" builder by the
// caller (splitIntoChunks) instead of being flushed immediately, so it can
// still be joined with subsequent lines.
func hardSplit(line string, maxLen int) []string {
	runes := []rune(line)
	if len(runes) <= maxLen {
		return []string{line}
	}
	var parts []string
	for len(runes) > 0 {
		n := maxLen
		if n > len(runes) {
			n = len(runes)
		}
		parts = append(parts, string(runes[:n]))
		runes = runes[n:]
	}
	return parts
}

func (h *Handler) audit(decision, reason string, chunks, totalLen int, duration time.Duration) {
	if h.logger == nil {
		return
	}
	parts := []string{
		"chunks=" + strconv.Itoa(chunks),
		"total_len=" + strconv.Itoa(totalLen),
		"duration_ms=" + strconv.FormatInt(duration.Milliseconds(), 10),
	}
	if reason != "" {
		parts = append(parts, "reason="+reason)
	}
	h.logger.Log(audit.Event{
		Role:     "slack-notify",
		ToolName: "POST /v1/slack/notify",
		Decision: decision,
		Reason:   strings.Join(parts, " "),
	})
}
