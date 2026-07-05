package main

import (
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/githubapp"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/gitrelay"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/jqmcp"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/jquants"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/policy"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/slacknotify"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/tools"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/xmcp"
)

// runHealthcheck is invoked as `auth-proxy -healthcheck` from a Docker
// HEALTHCHECK CMD. The distroless nonroot base image ships no shell, curl, or
// wget, so the binary performs the self-GET /healthz itself and exits
// non-zero on any failure.
func runHealthcheck() {
	addr := os.Getenv("AUTH_PROXY_ADDR")
	if addr == "" {
		addr = ":18081"
	}
	// AUTH_PROXY_ADDR may be host:port (e.g. "0.0.0.0:18081" so the process
	// listens on all interfaces per ADR-024); the self-check always targets
	// 127.0.0.1, so keep only the ":port" suffix.
	if idx := strings.LastIndex(addr, ":"); idx >= 0 {
		addr = addr[idx:]
	}
	client := http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://127.0.0.1" + addr + "/healthz")
	if err != nil {
		os.Exit(1)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		os.Exit(1)
	}
	os.Exit(0)
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "-healthcheck" {
		runHealthcheck()
		return
	}
	addr := os.Getenv("AUTH_PROXY_ADDR")
	if addr == "" {
		addr = ":18081"
	}
	logger := audit.NewLogger(os.Stdout)
	handler := tools.NewHandler(policy.NewDevEngine(), logger)

	mux := http.NewServeMux()
	mux.Handle("/", handler.Routes())
	mountGitRelay(mux, logger)
	mountXMCP(mux, logger)
	mountSlackNotify(mux, logger)

	log.Printf("auth-proxy listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("auth-proxy: %v", err)
	}
}

// mountGitRelay wires the git Smart HTTP relay (ADR-020) only when a session
// token is configured and GitHub App credentials are available; otherwise it
// logs a non-sensitive reason and leaves the tools routes serving alone.
func mountGitRelay(mux *http.ServeMux, logger *audit.Logger) {
	sessionToken := os.Getenv("AUTH_PROXY_SESSION_TOKEN")
	if sessionToken == "" {
		log.Printf("git relay disabled: no session token configured")
		return
	}

	tokens, err := githubapp.NewTokenSourceFromEnv()
	if err != nil {
		log.Printf("git relay disabled: github app credentials unavailable")
		return
	}

	upstream := os.Getenv("GIT_RELAY_UPSTREAM")
	relay, err := gitrelay.NewHandler(sessionToken, tokens, upstream, logger)
	if err != nil {
		log.Printf("git relay disabled: handler construction failed")
		return
	}

	mux.Handle("/git/", relay.Routes())
}

// mountXMCP mounts the /mcp endpoint (ADR-023, ADR-027) when
// AUTH_PROXY_SESSION_TOKEN is configured and at least one of X_BEARER_TOKEN
// (x-mcp-readonly tools) or JQUANTS_REFRESH_TOKEN (jq.* tools, ADR-027) is
// available. The tools/list output reflects whichever credentials are
// actually configured: X only, J-Quants only, or both.
func mountXMCP(mux *http.ServeMux, logger *audit.Logger) {
	sessionToken := os.Getenv("AUTH_PROXY_SESSION_TOKEN")
	if sessionToken == "" {
		log.Printf("x-mcp disabled: AUTH_PROXY_SESSION_TOKEN not set")
		return
	}

	includeXTools := os.Getenv("X_BEARER_TOKEN") != ""
	if !includeXTools {
		log.Printf("x-mcp X tools disabled: X_BEARER_TOKEN not set")
	}

	var extras []xmcp.ExtraTool
	jqTokens, err := jquants.NewTokenSourceFromEnv()
	if err != nil {
		log.Printf("jquants tools disabled: JQUANTS_REFRESH_TOKEN not set")
	} else {
		extras = jqmcp.Tools(jqTokens)
	}

	if !includeXTools && len(extras) == 0 {
		log.Printf("x-mcp disabled: neither X_BEARER_TOKEN nor JQUANTS_REFRESH_TOKEN configured")
		return
	}

	handler, err := xmcp.NewHandlerWithOptions(sessionToken, logger, includeXTools, extras...)
	if err != nil {
		log.Printf("x-mcp disabled: handler construction failed")
		return
	}
	mux.Handle("/mcp", handler.Routes())
}

// mountSlackNotify mounts POST /v1/slack/notify (ADR-026) only when
// AUTH_PROXY_SESSION_TOKEN, SLACK_BOT_TOKEN, and SLACK_CHANNEL_ID are all
// configured; otherwise it logs a non-sensitive reason and leaves the route
// unmounted. Both Slack vars are optional at the platform level (the
// investment digest job is opt-in). SLACK_API_BASE_URL is an internal
// override (tests only) for the Slack Web API base URL.
func mountSlackNotify(mux *http.ServeMux, logger *audit.Logger) {
	sessionToken := os.Getenv("AUTH_PROXY_SESSION_TOKEN")
	botToken := os.Getenv("SLACK_BOT_TOKEN")
	channelID := os.Getenv("SLACK_CHANNEL_ID")
	apiBase := os.Getenv("SLACK_API_BASE_URL")
	if sessionToken == "" {
		log.Printf("slack-notify disabled: no session token configured")
		return
	}
	if botToken == "" {
		log.Printf("slack-notify disabled: SLACK_BOT_TOKEN not set")
		return
	}
	if channelID == "" {
		log.Printf("slack-notify disabled: SLACK_CHANNEL_ID not set")
		return
	}

	handler, err := slacknotify.NewHandler(sessionToken, botToken, channelID, apiBase, logger)
	if err != nil {
		log.Printf("slack-notify disabled: handler construction failed")
		return
	}
	mux.Handle("/v1/slack/notify", handler.Routes())
}
