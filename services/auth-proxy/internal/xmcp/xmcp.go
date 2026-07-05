// Package xmcp implements the x-mcp-readonly MCP protocol contract (ADR-015,
// ADR-023) as a Go handler inside auth-proxy. It exposes exactly four
// read-only tools for the X API (x.search_posts_recent, x.get_posts,
// x.get_users, x.get_users_by_username) over JSON-RPC 2.0 at POST /mcp.
//
// The X API credential (X_BEARER_TOKEN) is read from this process's
// environment only, per request, and is never forwarded to callers or
// logged. stdlib only, no third-party dependencies.
package xmcp

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
)

const (
	protocolVersion = "2025-03-26"
	serverName      = "x-mcp-readonly"
	serverVersion   = "0.2.0"

	defaultXAPIBaseURL = "https://api.x.com"

	jsonrpcParseError    = -32700
	jsonrpcMethodMissing = -32601
	jsonrpcInvalidParams = -32602
)

// redactionPattern mirrors config/policy.yaml's redaction_policy defaults so
// the server degrades gracefully even without project config.
type redactionPattern struct {
	name  string
	regex *regexp.Regexp
}

// defaultRedactionPatterns ports every entry under
// config/policy.yaml's redaction_policy.patterns, in the same order, using
// Go's RE2 regexp syntax (a straight port since all of the source patterns
// are RE2-compatible). Keep this list and the parity guard test
// (tests/test_redaction_parity.py) in sync: that test fails the build when a
// pattern is added to policy.yaml without being ported here.
var defaultRedactionPatterns = []redactionPattern{
	{
		// policy.yaml: redaction_policy.patterns[env_assignment]
		name:  "env_assignment",
		regex: regexp.MustCompile(`(?i)(api[_-]?key|secret|token|password)\s*=`),
	},
	{
		// policy.yaml: redaction_policy.patterns[private_key]
		name:  "private_key",
		regex: regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`),
	},
	{
		// policy.yaml: redaction_policy.patterns[bearer_token]
		name:  "bearer_token",
		regex: regexp.MustCompile(`Bearer\s+[A-Za-z0-9._~+/-]+=*`),
	},
	{
		// policy.yaml: redaction_policy.patterns[anthropic_key]
		name:  "anthropic_key",
		regex: regexp.MustCompile(`sk-ant-[A-Za-z0-9._-]+`),
	},
	{
		// policy.yaml: redaction_policy.patterns[claude_proxy_session_token]
		name:  "claude_proxy_session_token",
		regex: regexp.MustCompile(`cp_sess_[A-Za-z0-9._-]+`),
	},
}

func redact(text string) string {
	redacted := text
	for _, p := range defaultRedactionPatterns {
		redacted = p.regex.ReplaceAllString(redacted, "[REDACTED:"+p.name+"]")
	}
	return redacted
}

// Tool describes an MCP tool definition returned from tools/list.
type Tool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

var tools = []Tool{
	{
		Name:        "x.search_posts_recent",
		Description: "Search recent X posts (read-only).",
		InputSchema: map[string]any{
			"type": "object",
			"properties": map[string]any{
				"query":       map[string]any{"type": "string"},
				"max_results": map[string]any{"type": "integer", "minimum": 10, "maximum": 100, "default": 10},
			},
			"required": []string{"query"},
		},
	},
	{
		Name:        "x.get_posts",
		Description: "Get X posts by id (read-only).",
		InputSchema: map[string]any{
			"type": "object",
			"properties": map[string]any{
				"ids": map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
			},
			"required": []string{"ids"},
		},
	},
	{
		Name:        "x.get_users",
		Description: "Get X users by id (read-only).",
		InputSchema: map[string]any{
			"type": "object",
			"properties": map[string]any{
				"ids": map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
			},
			"required": []string{"ids"},
		},
	},
	{
		Name:        "x.get_users_by_username",
		Description: "Get X users by username (read-only).",
		InputSchema: map[string]any{
			"type": "object",
			"properties": map[string]any{
				"usernames": map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
			},
			"required": []string{"usernames"},
		},
	},
}

var toolNames = func() map[string]bool {
	names := make(map[string]bool, len(tools))
	for _, t := range tools {
		names[t.Name] = true
	}
	return names
}()

// ToolHandler executes an extra (non-X) tool call and returns its result.
// Implementations must never include credentials in the returned text.
type ToolHandler func(arguments map[string]any) ToolResult

// ExtraTool pairs a tool definition with its handler, allowing other
// credential holders (e.g. J-Quants, ADR-027) to register additional tools
// on the same /mcp endpoint without xmcp owning their credentials or HTTP
// logic. The server's tools/list output and tools/call routing simply
// reflect whichever extras were configured at construction.
type ExtraTool struct {
	Tool    Tool
	Handler ToolHandler
}

// Handler serves the x-mcp-readonly JSON-RPC endpoint, plus any extra tools
// registered at construction time (ADR-027).
type Handler struct {
	sessionToken  string
	logger        *audit.Logger
	httpClient    *http.Client
	includeXTools bool
	extraTools    []Tool
	extraHandlers map[string]ToolHandler
}

// NewHandler builds an xmcp Handler. sessionToken must be non-empty
// (fail-closed, same convention as gitrelay.NewHandler): every request to
// /mcp must present it as a Bearer token, protecting the same listener as
// gitrelay consistently. logger may be nil (audit becomes a no-op).
//
// By default the handler serves the built-in X tools. Callers that only
// want extra tools (e.g. J-Quants configured without X_BEARER_TOKEN) should
// use NewHandlerWithOptions with includeXTools=false so tools/list reflects
// what is actually configured.
func NewHandler(sessionToken string, logger *audit.Logger, extras ...ExtraTool) (*Handler, error) {
	return NewHandlerWithOptions(sessionToken, logger, true, extras...)
}

// NewHandlerWithOptions builds an xmcp Handler with control over whether the
// built-in X tools are included in tools/list and dispatch.
func NewHandlerWithOptions(sessionToken string, logger *audit.Logger, includeXTools bool, extras ...ExtraTool) (*Handler, error) {
	if sessionToken == "" {
		return nil, errors.New("xmcp: session token must not be empty")
	}
	h := &Handler{
		sessionToken:  sessionToken,
		logger:        logger,
		includeXTools: includeXTools,
		httpClient: &http.Client{
			Timeout: 20 * time.Second,
		},
		extraHandlers: make(map[string]ToolHandler, len(extras)),
	}
	for _, extra := range extras {
		h.extraTools = append(h.extraTools, extra.Tool)
		h.extraHandlers[extra.Tool.Name] = extra.Handler
	}
	return h, nil
}

// allTools returns the tool list this handler exposes via tools/list,
// reflecting includeXTools and any registered extras.
func (h *Handler) allTools() []Tool {
	result := make([]Tool, 0, len(tools)+len(h.extraTools))
	if h.includeXTools {
		result = append(result, tools...)
	}
	result = append(result, h.extraTools...)
	return result
}

func (h *Handler) hasTool(name string) bool {
	if h.includeXTools && toolNames[name] {
		return true
	}
	_, ok := h.extraHandlers[name]
	return ok
}

// Routes registers the handler's HTTP routes on a mux.
func (h *Handler) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /mcp", h.handlePost)
	mux.HandleFunc("/mcp", h.handleOther)
	return mux
}

func (h *Handler) handleOther(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost {
		// Should not happen: POST /mcp pattern is more specific and takes
		// priority, but guard defensively.
		h.handlePost(w, r)
		return
	}
	w.WriteHeader(http.StatusMethodNotAllowed)
}

type jsonrpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type jsonrpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type jsonrpcResponse struct {
	JSONRPC string        `json:"jsonrpc"`
	ID      any           `json:"id"`
	Result  any           `json:"result,omitempty"`
	Error   *jsonrpcError `json:"error,omitempty"`
}

func errorResponse(id any, code int, message string) jsonrpcResponse {
	return jsonrpcResponse{JSONRPC: "2.0", ID: id, Error: &jsonrpcError{Code: code, Message: message}}
}

func resultResponse(id any, result any) jsonrpcResponse {
	return jsonrpcResponse{JSONRPC: "2.0", ID: id, Result: result}
}

// authorize checks the Authorization: Bearer <token> header against the
// handler's session token using a constant-time comparison, matching
// gitrelay.Handler.authorize so /mcp is protected consistently with the
// other routes on this listener.
func (h *Handler) authorize(r *http.Request) bool {
	const prefix = "Bearer "
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(auth[len(prefix):]), []byte(h.sessionToken)) == 1
}

func (h *Handler) handlePost(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	if !h.authorize(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		h.writeJSON(w, http.StatusOK, errorResponse(nil, jsonrpcParseError, "parse error"))
		return
	}

	var req jsonrpcRequest
	if len(body) == 0 || json.Unmarshal(body, &req) != nil {
		h.writeJSON(w, http.StatusOK, errorResponse(nil, jsonrpcParseError, "parse error"))
		return
	}

	switch req.Method {
	case "initialize":
		h.writeJSON(w, http.StatusOK, resultResponse(req.ID, map[string]any{
			"protocolVersion": protocolVersion,
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo":      map[string]any{"name": serverName, "version": serverVersion},
		}))
		return
	case "notifications/initialized":
		w.WriteHeader(http.StatusNoContent)
		return
	case "tools/list":
		h.writeJSON(w, http.StatusOK, resultResponse(req.ID, map[string]any{"tools": h.allTools()}))
		return
	case "tools/call":
		h.handleToolsCall(w, req, start)
		return
	default:
		h.writeJSON(w, http.StatusOK, errorResponse(req.ID, jsonrpcMethodMissing, "unknown method: "+req.Method))
		return
	}
}

type toolsCallParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

func (h *Handler) handleToolsCall(w http.ResponseWriter, req jsonrpcRequest, start time.Time) {
	var params toolsCallParams
	if len(req.Params) > 0 {
		_ = json.Unmarshal(req.Params, &params)
	}
	if params.Arguments == nil {
		params.Arguments = map[string]any{}
	}

	if !h.hasTool(params.Name) {
		h.writeJSON(w, http.StatusOK, errorResponse(req.ID, jsonrpcInvalidParams, "unknown or unsupported tool: "+params.Name))
		return
	}

	result := h.callTool(params.Name, params.Arguments)
	h.audit(params.Name, result.upstreamStatus, time.Since(start))
	h.writeJSON(w, http.StatusOK, resultResponse(req.ID, result.toResultMap()))
}

type ToolResult struct {
	text           string
	isError        bool
	upstreamStatus int
}

// Text returns the result's text payload, for use by tests of extra tool
// handlers registered from other packages.
func (t ToolResult) Text() string { return t.text }

// IsError reports whether this result represents a tool call failure, for
// use by tests of extra tool handlers registered from other packages.
func (t ToolResult) IsError() bool { return t.isError }

func (t ToolResult) toResultMap() map[string]any {
	m := map[string]any{
		"content": []map[string]any{
			{"type": "text", "text": t.text},
		},
	}
	if t.isError {
		m["isError"] = true
	}
	return m
}

func errorTextResult(text string) ToolResult {
	return ToolResult{text: text, isError: true}
}

// NewTextResult builds a successful ToolResult carrying a text payload
// (typically JSON), for use by extra tool handlers registered from other
// packages (ADR-027).
func NewTextResult(text string, upstreamStatus int) ToolResult {
	return ToolResult{text: text, upstreamStatus: upstreamStatus}
}

// NewErrorResult builds a failing ToolResult carrying only a non-sensitive
// error message and upstream status, for use by extra tool handlers
// registered from other packages. Callers must never pass credentials.
func NewErrorResult(text string, upstreamStatus int) ToolResult {
	return ToolResult{text: text, isError: true, upstreamStatus: upstreamStatus}
}

func (h *Handler) callTool(name string, arguments map[string]any) ToolResult {
	if handler, ok := h.extraHandlers[name]; ok {
		return handler(arguments)
	}

	token := os.Getenv("X_BEARER_TOKEN")
	if token == "" {
		return errorTextResult("X_BEARER_TOKEN is not configured")
	}

	switch name {
	case "x.search_posts_recent":
		query, _ := arguments["query"].(string)
		maxResults := intArg(arguments["max_results"], 10)
		payload, status, err := h.xAPIGet(token, "/2/tweets/search/recent", url.Values{
			"query":        {query},
			"max_results":  {strconv.Itoa(maxResults)},
			"tweet.fields": {"created_at,public_metrics,entities,author_id"},
			"expansions":   {"author_id"},
			"user.fields":  {"username"},
		})
		if err != nil {
			return xAPIErrorResult(err, status)
		}
		posts := normalizePosts(payload)
		return jsonResult(map[string]any{"posts": posts}, status)
	case "x.get_posts":
		ids := stringSliceArg(arguments["ids"])
		payload, status, err := h.xAPIGet(token, "/2/tweets", url.Values{
			"ids":          {strings.Join(ids, ",")},
			"tweet.fields": {"created_at,public_metrics,entities,author_id"},
			"expansions":   {"author_id"},
			"user.fields":  {"username"},
		})
		if err != nil {
			return xAPIErrorResult(err, status)
		}
		posts := normalizePosts(payload)
		return jsonResult(map[string]any{"posts": posts}, status)
	case "x.get_users":
		ids := stringSliceArg(arguments["ids"])
		payload, status, err := h.xAPIGet(token, "/2/users", url.Values{
			"ids":         {strings.Join(ids, ",")},
			"user.fields": {"username,name,public_metrics"},
		})
		if err != nil {
			return xAPIErrorResult(err, status)
		}
		users := normalizeUsers(payload)
		return jsonResult(map[string]any{"users": users}, status)
	case "x.get_users_by_username":
		usernames := stringSliceArg(arguments["usernames"])
		payload, status, err := h.xAPIGet(token, "/2/users/by", url.Values{
			"usernames":   {strings.Join(usernames, ",")},
			"user.fields": {"username,name,public_metrics"},
		})
		if err != nil {
			return xAPIErrorResult(err, status)
		}
		users := normalizeUsers(payload)
		return jsonResult(map[string]any{"users": users}, status)
	default:
		return errorTextResult("unknown tool: " + name)
	}
}

func jsonResult(v map[string]any, upstreamStatus int) ToolResult {
	b, err := json.Marshal(v)
	if err != nil {
		return ToolResult{text: "internal error", isError: true, upstreamStatus: upstreamStatus}
	}
	return ToolResult{text: string(b), upstreamStatus: upstreamStatus}
}

type xAPIError struct {
	status int
	title  string
}

func (e *xAPIError) Error() string {
	return fmt.Sprintf("X API error %d: %s", e.status, e.title)
}

func xAPIErrorResult(err error, status int) ToolResult {
	var apiErr *xAPIError
	if e, ok := err.(*xAPIError); ok {
		apiErr = e
	} else {
		apiErr = &xAPIError{status: status, title: err.Error()}
	}
	return ToolResult{
		text:           fmt.Sprintf("X API error (status=%d): %s", apiErr.status, apiErr.title),
		isError:        true,
		upstreamStatus: apiErr.status,
	}
}

func xAPIBaseURL() string {
	base := os.Getenv("X_API_BASE_URL")
	if base == "" {
		base = defaultXAPIBaseURL
	}
	return strings.TrimRight(base, "/")
}

func (h *Handler) xAPIGet(token, path string, params url.Values) (map[string]any, int, error) {
	endpoint := xAPIBaseURL() + path + "?" + params.Encode()
	req, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, 0, &xAPIError{status: 0, title: err.Error()}
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := h.httpClient.Do(req)
	if err != nil {
		return nil, 0, &xAPIError{status: 0, title: err.Error()}
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, &xAPIError{status: resp.StatusCode, title: "failed to read upstream response"}
	}

	if resp.StatusCode >= 300 {
		title := "X API request failed"
		var errBody map[string]any
		if json.Unmarshal(body, &errBody) == nil {
			if errs, ok := errBody["errors"].([]any); ok && len(errs) > 0 {
				if first, ok := errs[0].(map[string]any); ok {
					if t, ok := first["title"].(string); ok && t != "" {
						title = t
					} else if m, ok := first["message"].(string); ok && m != "" {
						title = m
					}
				}
			} else if t, ok := errBody["title"].(string); ok && t != "" {
				title = t
			} else if d, ok := errBody["detail"].(string); ok && d != "" {
				title = d
			}
		}
		return nil, resp.StatusCode, &xAPIError{status: resp.StatusCode, title: title}
	}

	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, resp.StatusCode, &xAPIError{status: resp.StatusCode, title: "invalid upstream response"}
	}
	return payload, resp.StatusCode, nil
}

func intArg(v any, def int) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case string:
		if parsed, err := strconv.Atoi(n); err == nil {
			return parsed
		}
	}
	return def
}

func stringSliceArg(v any) []string {
	items, ok := v.([]any)
	if !ok {
		return nil
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		if s, ok := item.(string); ok {
			result = append(result, s)
		}
	}
	return result
}

func postURL(postID, username string) string {
	handle := username
	if handle == "" {
		handle = "i/web"
	}
	return fmt.Sprintf("https://x.com/%s/status/%s", handle, postID)
}

func extractURLs(entities map[string]any) []string {
	if entities == nil {
		return []string{}
	}
	rawURLs, ok := entities["urls"].([]any)
	if !ok {
		return []string{}
	}
	result := make([]string, 0, len(rawURLs))
	for _, item := range rawURLs {
		obj, ok := item.(map[string]any)
		if !ok {
			continue
		}
		expanded, _ := obj["expanded_url"].(string)
		if expanded == "" {
			expanded, _ = obj["url"].(string)
		}
		if expanded != "" {
			result = append(result, expanded)
		}
	}
	return result
}

// jst is Asia/Tokyo, matching src/shichimimi_agent/util/time.py.
var jst = mustLoadJST()

func mustLoadJST() *time.Location {
	loc, err := time.LoadLocation("Asia/Tokyo")
	if err != nil {
		return time.FixedZone("JST", 9*60*60)
	}
	return loc
}

func isoNowJST() string {
	return time.Now().In(jst).Format("2006-01-02T15:04:05-07:00")
}

func normalizePosts(payload map[string]any) []map[string]any {
	data := asObjectSlice(payload["data"])

	usersByID := map[string]map[string]any{}
	if includes, ok := payload["includes"].(map[string]any); ok {
		for _, u := range asObjectSlice(includes["users"]) {
			if id, ok := u["id"].(string); ok && id != "" {
				usersByID[id] = u
			}
		}
	}

	posts := make([]map[string]any, 0, len(data))
	for _, post := range data {
		postID, _ := post["id"].(string)
		authorID, _ := post["author_id"].(string)
		username := ""
		if authorID != "" {
			if author, ok := usersByID[authorID]; ok {
				username, _ = author["username"].(string)
			}
		}
		metrics, _ := post["public_metrics"].(map[string]any)
		text, _ := post["text"].(string)
		createdAt, _ := post["created_at"].(string)
		entities, _ := post["entities"].(map[string]any)

		posts = append(posts, map[string]any{
			"id":            postID,
			"url":           postURL(postID, username),
			"author_handle": username,
			"created_at":    createdAt,
			"text_redacted": redact(text),
			"urls":          extractURLs(entities),
			"topics":        []string{},
			"engagement": map[string]any{
				"like_count":   metricInt(metrics, "like_count"),
				"repost_count": metricInt(metrics, "retweet_count"),
				"reply_count":  metricInt(metrics, "reply_count"),
				"quote_count":  metricInt(metrics, "quote_count"),
			},
			"collected_at": isoNowJST(),
		})
	}
	return posts
}

func normalizeUsers(payload map[string]any) []map[string]any {
	data := asObjectSlice(payload["data"])
	users := make([]map[string]any, 0, len(data))
	for _, user := range data {
		id, _ := user["id"].(string)
		username, _ := user["username"].(string)
		name, _ := user["name"].(string)
		metrics, _ := user["public_metrics"].(map[string]any)
		users = append(users, map[string]any{
			"id":              id,
			"username":        username,
			"name":            name,
			"followers_count": metricInt(metrics, "followers_count"),
			"following_count": metricInt(metrics, "following_count"),
		})
	}
	return users
}

func metricInt(metrics map[string]any, key string) int {
	if metrics == nil {
		return 0
	}
	if v, ok := metrics[key].(float64); ok {
		return int(v)
	}
	return 0
}

// asObjectSlice normalizes payload["data"], which the X API may return as
// either a single object or an array of objects, into a slice.
func asObjectSlice(v any) []map[string]any {
	switch val := v.(type) {
	case []any:
		result := make([]map[string]any, 0, len(val))
		for _, item := range val {
			if obj, ok := item.(map[string]any); ok {
				result = append(result, obj)
			}
		}
		return result
	case map[string]any:
		return []map[string]any{val}
	default:
		return nil
	}
}

func (h *Handler) writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (h *Handler) audit(toolName string, upstreamStatus int, duration time.Duration) {
	if h.logger == nil {
		return
	}
	reason := "duration_ms=" + strconv.FormatInt(duration.Milliseconds(), 10)
	if upstreamStatus != 0 {
		reason = "upstream_status=" + strconv.Itoa(upstreamStatus) + " " + reason
	}
	h.logger.Log(audit.Event{
		Role:     "x-mcp",
		ToolName: toolName,
		Decision: "allow",
		Reason:   reason,
	})
}
