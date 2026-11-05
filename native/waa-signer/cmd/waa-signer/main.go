// Local-only SpiderMonkey service. Account lifetime belongs to the Python lease.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
	_ "time/tzdata"

	"github.com/Mag1cFall/AIStudio2API/internal/aistudio"
	"github.com/Mag1cFall/AIStudio2API/internal/camoufoxnative"
	"github.com/Mag1cFall/AIStudio2API/internal/localapi"
	"github.com/Mag1cFall/AIStudio2API/internal/waa/mozjs"
)

const rpcBase = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/"

var buildCommit = "development"

func selfTest() bool {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	cx, err := mozjs.NewContext(mozjs.Options{HeapBytes: 32 << 20, Locale: "en-US"})
	if err != nil {
		return false
	}
	defer cx.Close()
	global, err := cx.NewGlobal(0, "UTC")
	if err != nil {
		return false
	}
	defer cx.Release(global)
	value, err := cx.Evaluate(global, "1 + 1", "offline-self-test", 1)
	return err == nil && value.Number == 2
}

type nativeWorker struct {
	worker    *aistudio.NativeWorker
	directory string
	tier      string
}

func (w *nativeWorker) Mint(ctx context.Context, digest, prompt string) (string, error) {
	proof, _, err := w.worker.ProofByDigest(ctx, digest, prompt)
	return proof, err
}
func (w *nativeWorker) State(ctx context.Context) (json.RawMessage, error) {
	state, err := w.worker.BrowserStorageState(ctx)
	if err != nil {
		return nil, err
	}
	return json.Marshal(state)
}
func (w *nativeWorker) Close() error {
	if err := w.worker.Close(); err != nil {
		return err
	}
	return os.RemoveAll(w.directory) // factory owns only this validated session subdirectory
}
func (w *nativeWorker) Send(ctx context.Context, method string, body []byte) (*http.Response, error) {
	headers, err := w.worker.ProtocolHeaders(ctx, "")
	if err != nil {
		return nil, err
	}
	state, err := w.worker.BrowserStorageState(ctx)
	if err != nil {
		return nil, err
	}
	auth, err := aistudio.NewSigner().Authorization(state)
	if err != nil {
		return nil, err
	}
	headers.Set("Authorization", auth)
	headers.Set("Content-Type", "application/json+protobuf")
	if w.tier != "" && (method == "GenerateContent" || method == "CodeAssistantOffline") {
		headers.Set("X-AIStudio-G1-Tier", w.tier)
	}
	response, err := w.worker.SendProtected(ctx, aistudio.ProtectedRequest{
		URL: rpcBase + method, Headers: headers, Body: body,
	})
	if err != nil {
		return nil, err
	}
	return &http.Response{StatusCode: response.StatusCode, Header: response.Header, Body: response.Body}, nil
}

func main() {
	selfTestFlag := flag.Bool("self-test", false, "evaluate synthetic JavaScript without network or credentials")
	listen := flag.String("listen", "127.0.0.1:8081", "loopback HTTP address")
	stateDir := flag.String("state-dir", "", "private state directory outside source control")
	proxy := flag.String("proxy", "", "existing upstream proxy, if required")
	flag.Parse()
	if *selfTestFlag {
		ok := selfTest()
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": ok, "commit": buildCommit, "googleContacted": false})
		if !ok {
			os.Exit(1)
		}
		return
	}
	host, _, err := net.SplitHostPort(*listen)
	ip := net.ParseIP(host)
	if err != nil || ip == nil || !ip.IsLoopback() || *stateDir == "" {
		os.Exit(2)
	}
	if err := os.MkdirAll(*stateDir, 0o700); err != nil {
		os.Exit(2)
	}
	// The imported runtime's free-form errors are never sent to journals or clients.
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	api := localapi.New(func(ctx context.Context, id string, cookies json.RawMessage, authUser int) (localapi.Worker, error) {
		state, err := aistudio.ParseCookieText(string(cookies))
		if err != nil {
			return nil, err
		}
		directory := filepath.Join(*stateDir, id)
		if err := os.MkdirAll(directory, 0o700); err != nil {
			return nil, err
		}
		path := filepath.Join(directory, "storage-state.json")
		if err := aistudio.WriteStorageState(path, state); err != nil {
			return nil, err
		}
		if err := os.Chmod(path, 0o600); err != nil {
			return nil, err
		}
		worker, err := aistudio.NewSpiderMonkeyWorker(ctx, id, camoufoxnative.Options{
			StorageStatePath: path, Model: "gemini-3.7-flash", BootstrapPrompt: camoufoxnative.DefaultBootstrapPrompt,
			Locale: "en-US", Timezone: "America/Los_Angeles", AuthUser: authUser, Proxy: *proxy,
		})
		if err != nil {
			_ = os.RemoveAll(directory)
			return nil, err
		}
		owned := &nativeWorker{worker: worker, directory: directory}
		response, err := owned.Send(ctx, "GetAiStudioBenefitTier", []byte("[]"))
		if err != nil {
			_ = owned.Close()
			return nil, err
		}
		defer response.Body.Close()
		var tier []int
		if response.StatusCode != 200 || json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&tier) != nil {
			_ = owned.Close()
			return nil, fmt.Errorf("benefit metadata unavailable")
		}
		if len(tier) > 0 {
			switch tier[0] {
			case 1:
				owned.tier = "TIER1"
			case 2:
				owned.tier = "TIER2"
			case 3:
				owned.tier = "TIER0"
			}
		}
		return owned, nil
	}, 2)
	server := &http.Server{Addr: *listen, Handler: api, ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	stop := make(chan os.Signal, 1)
	shutdownDone := make(chan struct{})
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		defer close(shutdownDone)
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if server.Shutdown(ctx) != nil {
			_ = server.Close()
		}
	}()
	fmt.Println("local WAA signer ready")
	err = server.ListenAndServe()
	if err == http.ErrServerClosed {
		<-shutdownDone
	}
	api.Close()
	if err != nil && err != http.ErrServerClosed {
		os.Exit(1)
	}
}
