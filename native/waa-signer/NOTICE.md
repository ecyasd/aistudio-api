# Source and scope

The `internal/aistudio`, `internal/camoufoxnative`, `internal/proxydial` and
`internal/waa` source and embedded fingerprint data are adapted from
[DeconstructedCube/aistudio-api, feat/browserless-waa-engine](https://github.com/DeconstructedCube/aistudio-api/tree/1ca65c1ee90788753fff4a6746c180c71cacdf1e/waa-signer),
whose signer extracts the core of
[Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API).
The Mag1cFall MIT license is retained in `LICENSE`.

SpiderMonkey and the Firefox 152 DOM/WebIDL host run Google's WAA JavaScript;
the Firefox uTLS/HTTP transport and account fingerprint use the same runtime.
The application does not create a replacement proof or bypass Google login.
Camoufox and go-browserforge supply the original fingerprint work and data.
Transitive Go modules retain their respective licenses.

The `cmd/waa-signer` and `internal/localapi` integration replaces the reference
HTTP server: loopback only, explicit per-generation session handles, two bounded
workers, no public signer relay, fixed MakerSuite methods, explicit stream EOF,
and owned session shutdown. Cookies remain in private runtime state; free-form
runtime logs are suppressed. Only the Python request owner controls retirement.

The `/v1/proof/mint` endpoint follows the local proof-service design pioneered by
[Lianues/aistudio-pg-bridge](https://github.com/Lianues/aistudio-pg-bridge).
This integration adds an explicit `session` handle and a digest; it is not a
drop-in server for the reference branch's cookie-per-request payload.
spinsnow is a protocol/environment reference; this project sends no data to its
public relay.

The Build assistant protocol in `build_assistant.py` is independently adapted
from the same MIT project's `internal/aistudio/assistant.go` and `docs/build.md`.
Google's built-in Build system instructions remain effective.
