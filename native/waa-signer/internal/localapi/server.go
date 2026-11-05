// Package localapi exposes only the RPCs used by the Python gateway.
package localapi

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"
)

const MaxBody = 16 << 20

var sessionID = regexp.MustCompile(`^[a-f0-9]{32}$`)
var digestID = regexp.MustCompile(`^[a-f0-9]{64}$`)
var Methods = map[string]bool{
	"GenerateContent": true, "ListModels": true,
	"CreateApplet": true, "DeleteApplet": true, "CodeAssistantOffline": true,
	"StreamCodeAssistantOfflineGeneration": true, "CancelCodeAssistantOfflineGeneration": true,
}

type Worker interface {
	Mint(context.Context, string, string) (string, error)
	Send(context.Context, string, []byte) (*http.Response, error)
	State(context.Context) (json.RawMessage, error)
	Close() error
}

type Factory func(context.Context, string, json.RawMessage, int) (Worker, error)

type entry struct {
	init    sync.Mutex
	worker  Worker
	users   int
	closing bool
}

type Server struct {
	mu      sync.Mutex
	entries map[string]*entry
	Factory Factory
	Limit   int
}

type request struct {
	Session  string          `json:"session"`
	Cookies  json.RawMessage `json:"cookies"`
	AuthUser int             `json:"authUser"`
	Digest   string          `json:"digest"`
	Prompt   string          `json:"pagePrompt"`
	Method   string          `json:"method"`
	Body     json.RawMessage `json:"body"`
}

func New(factory Factory, limit int) *Server {
	return &Server{Factory: factory, Limit: limit, entries: make(map[string]*entry)}
}

func write(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func failure(w http.ResponseWriter, status int) {
	write(w, status, map[string]any{"ok": false, "error": "local_signer_failure"})
}

func (s *Server) acquire(id string, create bool) (*entry, func(), error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e := s.entries[id]
	if e != nil && e.closing {
		return nil, nil, errors.New("session_closing")
	}
	if e == nil {
		if !create || len(s.entries) >= s.Limit {
			return nil, nil, errors.New("session_unavailable")
		}
		e = &entry{}
		s.entries[id] = e
	}
	e.users++
	return e, func() { s.mu.Lock(); e.users--; s.mu.Unlock() }, nil
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// No browser-origin access, redirects, arbitrary destinations, or public CORS.
	if r.Header.Get("Origin") != "" {
		failure(w, 403)
		return
	}
	if r.Method == "GET" && r.URL.Path == "/health" {
		s.mu.Lock()
		count := len(s.entries)
		s.mu.Unlock()
		write(w, 200, map[string]any{"ok": true, "engine": "spidermonkey-152", "workers": count})
		return
	}
	if r.Method != "POST" || !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
		failure(w, 400)
		return
	}
	var req request
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, MaxBody))
	if decoder.Decode(&req) != nil || !sessionID.MatchString(req.Session) ||
		decoder.Decode(new(any)) != io.EOF {
		failure(w, 400)
		return
	}
	if r.URL.Path == "/v1/session/close" {
		s.mu.Lock()
		e := s.entries[req.Session]
		if e != nil && (e.users != 0 || e.closing) {
			s.mu.Unlock()
			failure(w, 409)
			return
		}
		if e != nil {
			e.closing = true
		}
		s.mu.Unlock()
		var closeError error
		if e != nil && e.worker != nil {
			closeError = e.worker.Close()
		}
		s.mu.Lock()
		if closeError == nil {
			delete(s.entries, req.Session)
		} else {
			e.closing = false
		}
		s.mu.Unlock()
		if closeError != nil {
			failure(w, 503)
			return
		}
		write(w, 200, map[string]bool{"ok": true})
		return
	}
	opening := r.URL.Path == "/v1/session/open"
	if !opening && r.URL.Path != "/v1/proof/mint" && r.URL.Path != "/v1/rpc/stream" &&
		r.URL.Path != "/v1/session/state" {
		failure(w, 404)
		return
	}
	if opening && (len(req.Cookies) == 0 || req.AuthUser < 0 || req.AuthUser > 999) {
		failure(w, 400)
		return
	}
	e, release, err := s.acquire(req.Session, opening)
	if err != nil {
		failure(w, 503)
		return
	}
	defer release()
	e.init.Lock()
	if e.worker == nil && opening {
		ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
		e.worker, err = s.Factory(ctx, req.Session, req.Cookies, req.AuthUser)
		cancel()
	}
	worker := e.worker
	e.init.Unlock()
	if err != nil || worker == nil {
		failure(w, 503)
		return
	}
	switch r.URL.Path {
	case "/v1/session/open":
		write(w, 200, map[string]bool{"ok": true})
	case "/v1/session/state":
		state, err := worker.State(r.Context())
		if err != nil {
			failure(w, 503)
			return
		}
		write(w, 200, map[string]any{"ok": true, "state": state})
	case "/v1/proof/mint":
		if !digestID.MatchString(req.Digest) {
			failure(w, 400)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		proof, err := worker.Mint(ctx, req.Digest, req.Prompt)
		if err != nil || proof == "" {
			failure(w, 503)
			return
		}
		write(w, 200, map[string]any{"ok": true, "proof": proof})
	case "/v1/rpc/stream":
		if !Methods[req.Method] || len(req.Body) == 0 || req.Body[0] != '[' {
			failure(w, 400)
			return
		}
		response, err := worker.Send(r.Context(), req.Method, req.Body)
		if err != nil {
			failure(w, 503)
			return
		}
		defer response.Body.Close()
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.Header().Set("Cache-Control", "no-store")
		encoder := json.NewEncoder(w)
		emit := func(value any) bool {
			if encoder.Encode(value) != nil {
				return false
			}
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
			return true
		}
		if !emit(map[string]int{"status": response.StatusCode}) {
			return
		}
		buffer := make([]byte, 16<<10)
		for {
			n, err := response.Body.Read(buffer)
			if n > 0 && !emit(map[string]string{"chunk": base64.StdEncoding.EncodeToString(buffer[:n])}) {
				return
			}
			if err == io.EOF {
				emit(map[string]bool{"done": true})
				return
			}
			if err != nil {
				emit(map[string]string{"error": "upstream_read_failed"})
				return
			}
		}
	}
}

func (s *Server) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, e := range s.entries {
		if e.worker != nil {
			_ = e.worker.Close()
		}
	}
	s.entries = make(map[string]*entry)
}
