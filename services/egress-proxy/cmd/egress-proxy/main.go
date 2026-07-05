package main

import (
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/config"
	"github.com/7milch/7mimi-agent/services/egress-proxy/internal/proxy"
)

// runHealthcheck is invoked as `egress-proxy -healthcheck` from a Docker
// HEALTHCHECK CMD. The distroless nonroot base image ships no shell, curl,
// or wget, so the binary performs the self-GET /healthz itself and exits
// non-zero on any failure.
func runHealthcheck() {
	addr := os.Getenv("EGRESS_PROXY_ADDR")
	if addr == "" {
		addr = ":18082"
	}
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
	cfg := config.FromEnv()
	handler := proxy.NewHandler(cfg, audit.NewLogger(os.Stdout))
	log.Printf("egress-proxy listening on %s (deny=%v allow=%v)", cfg.Addr, cfg.DenyHosts, cfg.AllowHosts)
	if err := http.ListenAndServe(cfg.Addr, handler); err != nil {
		log.Fatalf("egress-proxy: %v", err)
	}
}
