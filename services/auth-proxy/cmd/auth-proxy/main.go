package main

import (
	"log"
	"net/http"
	"os"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/githubapp"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/gitrelay"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/policy"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/tools"
)

func main() {
	addr := os.Getenv("AUTH_PROXY_ADDR")
	if addr == "" {
		addr = ":18081"
	}
	logger := audit.NewLogger(os.Stdout)
	handler := tools.NewHandler(policy.NewDevEngine(), logger)

	mux := http.NewServeMux()
	mux.Handle("/", handler.Routes())
	mountGitRelay(mux, logger)

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
