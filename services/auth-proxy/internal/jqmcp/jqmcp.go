// Package jqmcp registers J-Quants tools (jq.get_listed_info,
// jq.get_daily_quotes, jq.get_statements) as extra tools on xmcp's /mcp
// endpoint (ADR-027). J-Quants responses are passed through as structured
// evidence (no redaction, unlike X posts); only the idToken credential is
// kept out of logs and error messages.
package jqmcp

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/jquants"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/xmcp"
)

// maxResponseBytes caps the pass-through response size to guard against
// pathological upstream payloads.
const maxResponseBytes = 1 << 20 // ~1MB

// Tools returns the xmcp.ExtraTool registrations for the three J-Quants
// tools, backed by the given TokenSource.
func Tools(tokens *jquants.TokenSource) []xmcp.ExtraTool {
	return []xmcp.ExtraTool{
		{
			Tool: xmcp.Tool{
				Name:        "jq.get_listed_info",
				Description: "Get J-Quants listed company info for a stock code (read-only, structured evidence).",
				InputSchema: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"code": map[string]any{"type": "string"},
					},
					"required": []string{"code"},
				},
			},
			Handler: handleListedInfo(tokens),
		},
		{
			Tool: xmcp.Tool{
				Name:        "jq.get_daily_quotes",
				Description: "Get J-Quants daily quotes for a stock code (read-only, structured evidence).",
				InputSchema: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"code": map[string]any{"type": "string"},
						"from": map[string]any{"type": "string"},
						"to":   map[string]any{"type": "string"},
					},
					"required": []string{"code"},
				},
			},
			Handler: handleDailyQuotes(tokens),
		},
		{
			Tool: xmcp.Tool{
				Name:        "jq.get_statements",
				Description: "Get J-Quants financial statements for a stock code (read-only, structured evidence).",
				InputSchema: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"code": map[string]any{"type": "string"},
					},
					"required": []string{"code"},
				},
			},
			Handler: handleStatements(tokens),
		},
	}
}

func handleListedInfo(tokens *jquants.TokenSource) xmcp.ToolHandler {
	return func(arguments map[string]any) xmcp.ToolResult {
		code, _ := arguments["code"].(string)
		if code == "" {
			return xmcp.NewErrorResult("code argument is required", 0)
		}
		return get(tokens, "/v1/listed/info", url.Values{"code": {code}})
	}
}

func handleDailyQuotes(tokens *jquants.TokenSource) xmcp.ToolHandler {
	return func(arguments map[string]any) xmcp.ToolResult {
		code, _ := arguments["code"].(string)
		if code == "" {
			return xmcp.NewErrorResult("code argument is required", 0)
		}
		params := url.Values{"code": {code}}
		if from, ok := arguments["from"].(string); ok && from != "" {
			params.Set("from", from)
		}
		if to, ok := arguments["to"].(string); ok && to != "" {
			params.Set("to", to)
		}
		return get(tokens, "/v1/prices/daily_quotes", params)
	}
}

func handleStatements(tokens *jquants.TokenSource) xmcp.ToolHandler {
	return func(arguments map[string]any) xmcp.ToolResult {
		code, _ := arguments["code"].(string)
		if code == "" {
			return xmcp.NewErrorResult("code argument is required", 0)
		}
		return get(tokens, "/v1/fins/statements", url.Values{"code": {code}})
	}
}

// get performs an authenticated GET against the J-Quants API and returns the
// response body as-is (structured evidence, no normalization). Errors never
// contain the idToken or refresh token; only the upstream HTTP status is
// surfaced.
func get(tokens *jquants.TokenSource, path string, params url.Values) xmcp.ToolResult {
	idToken, err := tokens.IDToken()
	if err != nil {
		return xmcp.NewErrorResult("failed to obtain jquants credential", 0)
	}

	endpoint := tokens.APIBase() + path + "?" + params.Encode()
	req, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return xmcp.NewErrorResult("failed to build jquants request", 0)
	}
	req.Header.Set("Authorization", "Bearer "+idToken)

	client := tokens.HTTPClient()
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return xmcp.NewErrorResult("jquants request failed", 0)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return xmcp.NewErrorResult("failed to read jquants response", resp.StatusCode)
	}

	if resp.StatusCode >= 300 {
		return xmcp.NewErrorResult(fmt.Sprintf("jquants API error (status=%d)", resp.StatusCode), resp.StatusCode)
	}

	// Validate it's well-formed JSON before passing through (defense in
	// depth), but otherwise pass the body through unmodified as structured
	// evidence per ADR-027.
	var probe json.RawMessage
	if err := json.Unmarshal(body, &probe); err != nil {
		return xmcp.NewErrorResult("jquants response was not valid JSON", resp.StatusCode)
	}

	return xmcp.NewTextResult(strings.TrimSpace(string(body)), resp.StatusCode)
}
