package audit

import (
	"encoding/json"
	"io"
	"sync"
	"time"
)

// Event is metadata-only audit information. Request/response bodies and
// headers must never appear here.
type Event struct {
	Timestamp  string `json:"timestamp"`
	Method     string `json:"method"`
	Host       string `json:"host"`
	Port       string `json:"port"`
	Decision   string `json:"decision"`
	Reason     string `json:"reason,omitempty"`
	DurationMS int64  `json:"duration_ms"`
}

// Logger is safe for concurrent use: the proxy serves CONNECT tunnels and
// forwarded requests on separate goroutines per connection, all sharing one
// audit sink.
type Logger struct {
	mu  sync.Mutex
	out io.Writer
}

func NewLogger(out io.Writer) *Logger {
	return &Logger{out: out}
}

func (l *Logger) Log(event Event) {
	if event.Timestamp == "" {
		event.Timestamp = time.Now().UTC().Format(time.RFC3339Nano)
	}
	line, err := json.Marshal(event)
	if err != nil {
		return // fail-open: audit must not break proxying
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.out.Write(append(line, '\n'))
}
