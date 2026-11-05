package localapi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeWorker struct {
	closed     bool
	readError  bool
	closeError bool
}

func (f *fakeWorker) Mint(context.Context, string, string) (string, error) {
	return "synthetic-proof", nil
}
func (f *fakeWorker) State(context.Context) (json.RawMessage, error) {
	return json.RawMessage(`{"cookies":[]}`), nil
}
func (f *fakeWorker) Close() error {
	if f.closeError {
		return errors.New("synthetic close failure")
	}
	f.closed = true
	return nil
}
func (f *fakeWorker) Send(context.Context, string, []byte) (*http.Response, error) {
	var source io.Reader = strings.NewReader("synthetic-upstream")
	if f.readError {
		source = &failedReader{}
	}
	return &http.Response{StatusCode: 200, Body: io.NopCloser(source)}, nil
}

type failedReader struct{}

func (*failedReader) Read([]byte) (int, error) {
	return 0, errors.New("synthetic raw credential must not escape")
}

func post(s *Server, path, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest("POST", path, strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}

const id = "0123456789abcdef0123456789abcdef"

func TestSessionLifetimeAndCapacity(t *testing.T) {
	worker := &fakeWorker{}
	s := New(func(context.Context, string, json.RawMessage, int) (Worker, error) { return worker, nil }, 1)
	if w := post(s, "/v1/session/open", `{"session":"`+id+`","cookies":{"cookies":[]}}`); w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	if w := post(s, "/v1/session/open", `{"session":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","cookies":{}}`); w.Code != 503 {
		t.Fatal(w.Code)
	}
	_, release, _ := s.acquire(id, false)
	if w := post(s, "/v1/session/close", `{"session":"`+id+`"}`); w.Code != 409 || worker.closed {
		t.Fatal("closed an owned worker")
	}
	release()
	if w := post(s, "/v1/session/close", `{"session":"`+id+`"}`); w.Code != 200 || !worker.closed {
		t.Fatal("worker was not released")
	}
}

func TestStreamDistinguishesEOFAndReadFailure(t *testing.T) {
	for _, readError := range []bool{false, true} {
		worker := &fakeWorker{readError: readError}
		s := New(func(context.Context, string, json.RawMessage, int) (Worker, error) { return worker, nil }, 2)
		post(s, "/v1/session/open", `{"session":"`+id+`","cookies":{}}`)
		w := post(s, "/v1/rpc/stream", `{"session":"`+id+`","method":"GenerateContent","body":[]}`)
		text := w.Body.String()
		if strings.Contains(text, `"done":true`) == readError || strings.Contains(text, "credential") {
			t.Fatal(text)
		}
		if !strings.Contains(text, `"status":200`) {
			t.Fatal(text)
		}
	}
}

func TestOnlyLocalProtocolAndFixedMethods(t *testing.T) {
	s := New(func(context.Context, string, json.RawMessage, int) (Worker, error) { return &fakeWorker{}, nil }, 2)
	post(s, "/v1/session/open", `{"session":"`+id+`","cookies":{}}`)
	if w := post(s, "/v1/rpc/stream", `{"session":"`+id+`","method":"https://attacker.invalid","body":[]}`); w.Code != 400 {
		t.Fatal(w.Code)
	}
	r := httptest.NewRequest("GET", "/health", nil)
	r.Header.Set("Origin", "https://attacker.invalid")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("browser origin accepted")
	}
	if w := post(s, "/v1/session/open", `{"session":"../bad","cookies":{}}`); w.Code != 400 {
		t.Fatal(w.Code)
	}
}

func TestCloseFailureDoesNotReportSuccessOrForgetWorker(t *testing.T) {
	worker := &fakeWorker{closeError: true}
	s := New(func(context.Context, string, json.RawMessage, int) (Worker, error) { return worker, nil }, 1)
	post(s, "/v1/session/open", `{"session":"`+id+`","cookies":{}}`)
	if w := post(s, "/v1/session/close", `{"session":"`+id+`"}`); w.Code != 503 || len(s.entries) != 1 {
		t.Fatal("failed cleanup was hidden")
	}
	worker.closeError = false
	if w := post(s, "/v1/session/close", `{"session":"`+id+`"}`); w.Code != 200 || !worker.closed {
		t.Fatal("owned cleanup could not finish")
	}
}
