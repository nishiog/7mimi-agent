// Package gitrelay implements the git Smart HTTP relay (ADR-020): a
// credential-free agent-runner is authorized via a static session bearer
// token, and auth-proxy injects a short-lived GitHub App installation token
// when forwarding to GitHub. Never logs Authorization headers, Basic
// credentials, App private keys, or installation tokens.
package gitrelay

import (
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/audit"
	"github.com/7milch/7mimi-agent/services/auth-proxy/internal/githubapp"
)

const defaultUpstream = "https://github.com"

var ownerRepoPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

// Handler serves the git Smart HTTP relay routes.
type Handler struct {
	sessionToken string
	tokens       *githubapp.TokenSource
	upstream     string
	logger       *audit.Logger
}

// NewHandler builds the relay handler. sessionToken must be non-empty
// (fail-closed; there is no default).
func NewHandler(sessionToken string, tokens *githubapp.TokenSource, upstream string, logger *audit.Logger) (*Handler, error) {
	if sessionToken == "" {
		return nil, errors.New("gitrelay: session token must not be empty")
	}
	if upstream == "" {
		upstream = defaultUpstream
	}
	return &Handler{
		sessionToken: sessionToken,
		tokens:       tokens,
		upstream:     strings.TrimRight(upstream, "/"),
		logger:       logger,
	}, nil
}

// Routes registers the relay's HTTP routes on a mux.
func (h *Handler) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /git/{owner}/{repo}/info/refs", h.handleInfoRefs)
	mux.HandleFunc("POST /git/{owner}/{repo}/git-upload-pack", h.handleService("git-upload-pack"))
	mux.HandleFunc("POST /git/{owner}/{repo}/git-receive-pack", h.handleService("git-receive-pack"))
	mux.HandleFunc("/git/", h.handleNotFound)
	return mux
}

func (h *Handler) authorize(w http.ResponseWriter, r *http.Request) bool {
	const prefix = "Bearer "
	auth := r.Header.Get("Authorization")
	ok := strings.HasPrefix(auth, prefix) &&
		subtle.ConstantTimeCompare([]byte(auth[len(prefix):]), []byte(h.sessionToken)) == 1
	if !ok {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return false
	}
	return true
}

func validOwnerRepo(owner, repo string) bool {
	// "." / ".." are syntactically valid segments for the pattern but would
	// produce a path-traversing upstream URL; reject them outright.
	for _, s := range []string{owner, repo} {
		if s == "." || s == ".." || !ownerRepoPattern.MatchString(s) {
			return false
		}
	}
	return true
}

func (h *Handler) handleNotFound(w http.ResponseWriter, r *http.Request) {
	http.NotFound(w, r)
}

func (h *Handler) handleInfoRefs(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	owner := r.PathValue("owner")
	repo := r.PathValue("repo")
	service := r.URL.Query().Get("service")

	if service != "git-upload-pack" && service != "git-receive-pack" {
		h.audit(r, owner, repo, "", "block", "invalid service parameter", 0, time.Since(start))
		http.Error(w, "invalid service parameter", http.StatusBadRequest)
		return
	}
	if !h.authorize(w, r) {
		h.audit(r, owner, repo, service, "block", "unauthorized", 0, time.Since(start))
		return
	}
	if !validOwnerRepo(owner, repo) {
		h.audit(r, owner, repo, service, "block", "invalid owner/repo", 0, time.Since(start))
		http.NotFound(w, r)
		return
	}

	h.proxy(w, r, owner, repo, "info/refs", service, start)
}

func (h *Handler) handleService(service string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		owner := r.PathValue("owner")
		repo := r.PathValue("repo")

		if !h.authorize(w, r) {
			h.audit(r, owner, repo, service, "block", "unauthorized", 0, time.Since(start))
			return
		}
		if !validOwnerRepo(owner, repo) {
			h.audit(r, owner, repo, service, "block", "invalid owner/repo", 0, time.Since(start))
			http.NotFound(w, r)
			return
		}

		h.proxy(w, r, owner, repo, service, service, start)
	}
}

func (h *Handler) proxy(w http.ResponseWriter, r *http.Request, owner, repo, upstreamSuffix, service string, start time.Time) {
	token, err := h.tokens.Token(r.Context())
	if err != nil {
		h.audit(r, owner, repo, service, "block", "token mint failed", 0, time.Since(start))
		http.Error(w, "upstream authentication unavailable", http.StatusBadGateway)
		return
	}

	upstreamURL, err := url.Parse(h.upstream)
	if err != nil {
		h.audit(r, owner, repo, service, "block", "invalid upstream", 0, time.Since(start))
		http.Error(w, "internal error", http.StatusBadGateway)
		return
	}

	upstreamHost := upstreamURL.Host
	statusCapture := &statusCapturingWriter{ResponseWriter: w, statusCode: http.StatusOK}

	rp := &httputil.ReverseProxy{
		FlushInterval: -1,
		Transport: &http.Transport{
			DialContext:           (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
			TLSHandshakeTimeout:   10 * time.Second,
			ResponseHeaderTimeout: 30 * time.Second,
			// Protocol-transparent relay: never let Go auto-negotiate/decompress
			// gzip on our behalf, or Content-Encoding/body bytes would diverge
			// from what upstream actually sent (concept doc: gzip 無変換透過).
			DisableCompression: true,
		},
		Director: func(req *http.Request) {
			req.URL.Scheme = upstreamURL.Scheme
			req.URL.Host = upstreamURL.Host
			req.Host = upstreamURL.Host
			req.URL.Path = upstreamURL.Path + "/" + owner + "/" + repo + ".git/" + upstreamSuffix

			req.Header.Del("Authorization")
			for name := range req.Header {
				if strings.HasPrefix(strings.ToLower(name), "x-7mimi-") {
					req.Header.Del(name)
				}
			}
			req.Header.Set("Authorization", "Basic "+basicAuth("x-access-token", token))
		},
		ModifyResponse: func(resp *http.Response) error {
			if resp.StatusCode >= 300 && resp.StatusCode < 400 {
				location := resp.Header.Get("Location")
				if location != "" {
					locURL, err := url.Parse(location)
					if err == nil && locURL.Host != "" && locURL.Host != upstreamHost {
						return errors.New("gitrelay: cross-host redirect blocked")
					}
					if err == nil {
						locURL.User = nil
						resp.Header.Set("Location", locURL.String())
					}
				}
			}
			statusCapture.statusCode = resp.StatusCode
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			http.Error(w, "bad gateway", http.StatusBadGateway)
		},
	}

	rp.ServeHTTP(statusCapture, r)
	h.audit(r, owner, repo, service, "allow", "", statusCapture.statusCode, time.Since(start))
}

type statusCapturingWriter struct {
	http.ResponseWriter
	statusCode int
}

func (w *statusCapturingWriter) WriteHeader(code int) {
	w.statusCode = code
	w.ResponseWriter.WriteHeader(code)
}

func basicAuth(username, password string) string {
	return base64.StdEncoding.EncodeToString([]byte(username + ":" + password))
}

func (h *Handler) audit(r *http.Request, owner, repo, service, decision, reason string, upstreamStatus int, duration time.Duration) {
	if h.logger == nil {
		return
	}
	h.logger.Log(audit.Event{
		Role:     "git-relay",
		ToolName: r.Method + " " + r.URL.Path,
		Decision: decision,
		Reason:   formatReason(owner, repo, service, reason, upstreamStatus, duration),
	})
}

func formatReason(owner, repo, service, reason string, upstreamStatus int, duration time.Duration) string {
	parts := []string{"owner=" + owner, "repo=" + repo, "service=" + service}
	if upstreamStatus != 0 {
		parts = append(parts, "upstream_status="+strconv.Itoa(upstreamStatus))
	}
	parts = append(parts, "duration_ms="+strconv.FormatInt(duration.Milliseconds(), 10))
	if reason != "" {
		parts = append(parts, "reason="+reason)
	}
	return strings.Join(parts, " ")
}
