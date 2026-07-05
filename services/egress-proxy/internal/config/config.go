package config

import (
	"os"
	"strings"
)

// Config holds egress-proxy runtime configuration.
type Config struct {
	Addr       string
	DenyHosts  []string
	AllowHosts []string
}

const defaultDenyHosts = "api.anthropic.com"

func FromEnv() *Config {
	return &Config{
		Addr:       envOr("EGRESS_PROXY_ADDR", ":18082"),
		DenyHosts:  splitCSV(envOr("EGRESS_DENY_HOSTS", defaultDenyHosts)),
		AllowHosts: splitCSV(os.Getenv("EGRESS_ALLOW_HOSTS")),
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func splitCSV(v string) []string {
	if v == "" {
		return nil
	}
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(strings.ToLower(p))
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}
