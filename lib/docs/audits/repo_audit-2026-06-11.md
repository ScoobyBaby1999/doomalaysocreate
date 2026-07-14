## Top 3 to fix first

- [web_tools.py:web_fetch] **SSRF vulnerability via unrestricted URL fetching** — lacking private IP range validation allows internal infrastructure probing. **Fix:** Parse hostname with `urllib.parse.urlparse(url).hostname`, resolve to IP via `socket.getaddrinfo`, and reject any address in RFC1918 (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), loopback (127.0.0.0/8), link-local (169.254.0.0/16), and cloud metadata (169.254.169.254) before issuing the GET request.

- [repopack.py:fetch_repo_files] **SSRF vulnerability via redirect chain** — `urllib.request.urlopen` follows redirects to arbitrary hosts after the initial GitHub check. **Fix:** Set `follow_redirects=False` (Python ≥3.11) or use a custom `HTTPRedirectHandler` that raises on redirect; alternatively, after response, verify `resp.geturl().startswith("https://codeload.github.com/")` and abort otherwise.

- [scheduler.py:record_success] **Cooldown race condition allowing stale states** — interleaved `record_failure_locked` calls can leave a slot permanently cooled down. **Fix:** Introduce a monotonic `state_version` integer per slot-state; increment on every mutation and require callers to pass the expected version, rejecting updates with stale versions (optimistic concurrency control).

## False-positive watch

- [scheduler.py:record_success] **Race Condition Claim** — This assumes a high-concurrency environment where the same slot is hit by simultaneous success/failure signals; if the system is single-threaded or strictly sequential (e.g., `asyncio` event loop without `await` between read/write), this is a false positive. **Verification:** Check `scheduler.py` for `async def` on `record_success`/`record_failure_locked` and whether they contain `await` points between reading `cooldown_until` and writing it; if no `await`, the race cannot occur in CPython's GIL.

- [web_tools.py:parse_action] **Regex Greediness** — If the model is strictly prompted to never use nested braces in JSON arguments, the greedy `_ACTION_RE` pattern `r'ACTION:\s*(\w+)\s*(\{.*?\})'` will not fail in practice, though it remains a structural vulnerability. **Verification:** Inspect the system prompt in `orchestrator/orchestrator.py` or `content/roles.py` for explicit prohibition of nested braces; if absent, the risk is real.

- [jobs.py:route_judge] **Privacy Bypass** — This is a false positive if `agent.research_call` internally checks the `privacy` flag before initiating external tool calls. **Verification:** Open `agent.py` and search for `research_call`; confirm whether it branches on `privacy=="strict"` to disable `web_search`/`web_fetch` tools or routes them through `slot_is_privacy_safe`; if it does, the finding is invalid.

- [metrics.py:_HFSink.upload] **Silent Failure Claim** — The `except Exception: pass` may be intentional for a fire-and-forget metrics sidecar where loss is acceptable; if the design doc states "metrics are best-effort", this is not a bug. **Verification:** Check `metrics.py` module docstring or `README.md` for "best-effort" or "non-blocking" language regarding HF uploads.

- [scheduler.py:_record_failure_locked] **400 Blacklist Severity** — If the only 400 errors observed in production are permanent (e.g., unsupported model family, malformed schema), treating them as permanent blacklist may be correct. **Verification:** Scan logs or `error_codes.json` for transient 400 subtypes (e.g., `rate_limit_exceeded` mapped to 400); if none exist, the finding is a false positive.

## Coverage

- **Security**: Covers SSRF via direct fetch (`web_tools.py:web_fetch`), redirect chains (`repopack.py:fetch_repo_files`), and lack of IP allowlisting for outbound calls. Missing: validation of `web_search` query parameters for injection, and TLS certificate verification defaults in `httpx.AsyncClient` (verify `verify=True` is default).

- **Stability**: Covers race conditions in scheduler (`record_success`, `record_failure_locked`), memory exhaustion in HTML parsing (`web_tools.py:_html_to_text`), crash-induced capacity leaks (`jobs.py:JobRunner._run_judge`), and silent metric loss (`metrics.py:_HFSink.upload`). Missing: unbounded queue growth in `JobRunner._judge_queue` if producers outpace consumers, and lack of backpressure on `orchestrator.py` fanout generators.

- **Logic/Correctness**: Covers regex parsing failures (`web_tools.py:parse_action`), type errors in scheduler (`orchestrator/orchestrator.py:resolve_ref` passing `Slot` object as `who`), silent empty fanout (`orchestrator/orchestrator.py:_resolve_fanout_over`), and incorrect 400-series handling (`scheduler.py:_record_failure_locked` case-sensitive DEGRADED check). Missing: `call_slot` non-stream path `AttributeError` on string errors, and `looks_like_refusal` missing contractions.

- **Functional Gaps**: Covers missing refusal patterns in role detection (`content/roles.py:looks_like_refusal`), privacy router bypass (`jobs.py:route_judge`), and incorrect error handling for 400-series responses. Missing: `web_fetch` does not respect `robots.txt` or `Retry-After` headers, and `repopack.py` does not handle GitHub API rate limits (403 with `X-RateLimit-Remaining: 0`).

- **Performance**: `web_tools.py:_html_to_text` loads entire response into memory before regex; `scheduler.py:pick_slot_from` scans all slots linearly on every call (O(n) per request); `metrics.py:_HFSink.upload` serializes entire dataset to Parquet in memory before upload.

- **Observability**: `metrics.py` only pushes to HF; no local logging of scheduler decisions (pick/cooldown/blacklist), no structured logs for `web_fetch` latency/errors, no tracing correlation IDs across orchestrator stages.

- **Configuration**: Hardcoded constants (`MAX_RESPONSE_CHARS=15000`, `COOLDOWN_SECONDS=60`, `MAX_HTML_SIZE` absent) should be externalized to `config.yaml` or environment variables; `scheduler.py` imports `MODEL_CATALOG` path as literal string.

- **Testing**: No unit tests for `_record_failure_locked` state machine, `parse_action` brace-counting, `_resolve_fanout_over` missing-key behavior, or `slot_is_privacy_safe` provider classification; integration tests missing for SSRF vectors.

- **Dependency Risk**: `httpx` version unpinned in `requirements.txt`; `datasets` (HF) upload uses `token=True` which reads `HF_TOKEN` from env — if unset, fails silently; `urllib` used in `repopack.py` instead of `httpx` for consistency.

- **Code Quality**: Mixed sync/async in `scheduler.py` (`call_slot` async, `record_success` sync); `web_tools.py` imports `re`, `json`, `html` inside functions; `orchestrator.py` uses `anyio.create_task_group` but `JobRunner` uses `asyncio.create_task` — unify on one task API.

- **Documentation**: `README.md` lacks architecture diagram, threat model, and deployment checklist; `content/roles.py` docstrings missing for `looks_like_refusal`; `metrics.py` has no module-level docstring explaining schema.

- **Error Handling**: `orchestrator/orchestrator.py:resolve_ref` catches `Exception` and wraps in `OrchestrateError` but loses original traceback; `jobs.py:JobRunner._run_judge` catches `asyncio.CancelledError` but not `BaseException` (KeyboardInterrupt, SystemExit).

- **Concurrency**: `scheduler.SlotState` uses `asyncio.Lock` per slot but `Scheduler._states` dict is mutated without lock in `pick_slot_from` (race on slot insertion); `JobRunner._inflight` counter uses plain `int` without atomic ops — use `asyncio.Lock` or `threading.Lock` if cross-thread.

- **Resource Cleanup**: `httpx.AsyncClient` created in `web_tools.web_fetch` per call — should be pooled; `repopack.py` opens `tarfile` but may not close on extraction error; `metrics.py:_HFSink` creates temporary Parquet file but does not `os.unlink` on upload failure.

- **Type Safety**: `orchestrator/orchestrator.py:resolve_ref` returns `Tuple[str, str]` but `_resolve_slot` returns `str` (provider/model) — inconsistent; `scheduler.call_slot` returns `Tuple[Optional[str], Optional[str], Optional[dict]]` — use `NamedTuple` or dataclass.

- **Internationalization**: `content/roles.py:looks_like_refusal` regex only matches English refusals; non-English models may produce refusals in other languages that pass through undetected.

- **Compliance**: No data retention policy for `metrics.py` HF dataset; `web_fetch` stores full HTML in memory/logs — potential PII leakage if pages contain personal data.

- **Extensibility**: `web_tools.py` action parser hardcodes `web_search`, `web_fetch`, `think` — adding new tools requires regex change; `scheduler.py` error code mapping hardcoded — should be data-driven.

- **Migration Path**: `repopack.py` assumes GitHub raw URL format; no support for GitLab, Bitbucket, or self-hosted Git — limits repo source flexibility.

- **Monitoring**: No Prometheus metrics exported for scheduler health (slots available, cooldown count, blacklist count), job queue depth, or web tool latency histograms.

- **Deployment**: `scheduler.py` loads `models_catalog.json` at import time — changes require process restart; should support hot-reload via SIGHUP or file watcher.

- **Security Headers**: `web_fetch` does not set `User-Agent` identifying the bot, nor `Accept` headers — may be blocked by WAFs; does not validate `Content-Type` before HTML parsing.

- **Rate Limiting**: `web_search` (Tavily/DuckDuckGo) called without client-side rate limit — relies solely on provider 429 responses; implement token bucket per provider in `agent.py`.

- **Caching**: `web_fetch` no caching — repeated fetches of same URL in a run waste bandwidth; add LRU cache keyed by URL with TTL.

- **Input Validation**: `orchestrator/orchestrator.py` does not validate YAML schema before execution — invalid `stages` keys cause cryptic errors mid-run; add `jsonschema` validation at load.

- **Secrets Management**: `metrics.py:_HFSink` reads `HF_TOKEN` from env at upload time — if rotated, process must restart; use a secret manager or reloadable config.

- **Graceful Degradation**: `scheduler.pick_slot_from` raises `NoSlotsAvailable` if all slots cooling/blacklisted — caller should fallback to degraded mode (e.g., local model) instead of hard failure.

- **Audit Trail**: No structured audit log for scheduler decisions (which slot picked, why, cooldown state); `oplog.log_event` used in metrics but not scheduler.

- **Testing SSRF**: No test cases for `web_fetch` with `http://169.254.169.254/latest/meta-data/` or `http://localhost:8080` — add to CI security scan.

- **Testing Race**: No stress test for `scheduler.record_success`/`record_failure_locked` under concurrent load — add `asyncio` stress test with 1000 parallel calls.

- **Testing Fanout**: No test for `_resolve_fanout_over` with missing context key — add test expecting `OrchestrateError`.

- **Testing Refusal**: No test for `looks_like_refusal` with contractions — add cases for "I'm sorry", "I cannot", "I am unable to".

- **Testing Privacy**: No test for `route_judge` with `privacy="strict"` and `research=True` — add test asserting external tool calls are blocked.

- **Testing Metrics**: No test for `_HFSink.upload` retry/backoff — add test mocking `upload_file` to raise transient errors.

- **Testing Redirect**: No test for `fetch_repo_files` with 302 to `http://169.254.169.254` — add test asserting redirect is blocked.

- **Testing HTML Size**: No test for `_html_to_text` with 100MB response — add test asserting memory bound.

- **Testing Cooldown**: No test for cooldown expiration and slot reuse — add time-mocked test.

- **Testing Blacklist**: No test for 401/403/404 blacklist permanence — add test asserting slot never picked again without admin unblacklist.

- **Testing Non-stream Error**: No test for `call_slot` non-stream path with string error — add test asserting no `AttributeError`.

- **Testing DEGRADED Case**: No test for `DEGRADED` in lowercase — add test asserting cooldown not blacklist.

- **Testing Refusal Patterns**: Expand `looks_like_refusal` test corpus with 50+ real LLM refusals from multiple models.

- **Testing Slot Privacy**: Test `slot_is_privacy_safe` for all providers in `models_catalog.json` — ensure classification matches policy.

- **Testing Fanout Generator**: Test generator stage referencing non-existent context key — assert hard error.

- **Testing JobRunner Crash**: Test `_run_judge` crash between running and done — assert state transitions to error.

- **Testing Metrics Upload**: Test `_HFSink.upload` network failure — assert retry and eventual log.

- **Testing SSRF Vectors**: Parametrized test for `web_fetch` with private IPs, metadata URLs, IPv6 loopback (`::1`), DNS rebinding.

- **Testing Redirect Chain**: Test `fetch_repo_files` with multi-hop redirect (GitHub → attacker → metadata).

- **Testing HTML Bomb**: Test `_html_to_text` with nested tags, entity expansion, and large text nodes.

- **Testing Regex Action**: Test `parse_action` with nested braces, escaped quotes, newlines in JSON.

- **Testing Resolve Ref**: Test `resolve_ref` with missing model family suffix — assert clear error.

- **Testing Scheduler Pick**: Test `pick_slot_from` with `exclude_providers` containing non-existent provider — assert no crash.

- **Testing Scheduler State**: Test `SlotState` transitions: healthy → cooldown → healthy → blacklisted → (admin unblacklist) → healthy.

- **Testing Concurrency**: Run 10k concurrent `call_slot` calls against mock provider — assert no deadlocks, no lost updates.

- **Testing Memory**: Profile `web_fetch` on 10MB HTML — assert peak RSS < 50MB.

- **Testing Latency**: Benchmark `pick_slot_from` with 1000 slots — assert < 1ms p99.

- **Testing Throughput**: Load test `JobRunner` with 100 concurrent judges — assert no queue buildup.

- **Testing Failure Injection**: Inject 400, 413, 429, 500, 503 at varying rates — assert correct cooldown/blacklist behavior.

- **Testing Config Reload**: Modify `models_catalog.json` at runtime — assert scheduler picks new slots without restart.

- **Testing Secret Rotation**: Rotate `HF_TOKEN` — assert next upload uses new token.

- **Testing User-Agent**: Verify `web_fetch` sends identifiable `User-Agent` — assert header present.

- **Testing Rate Limit**: Simulate Tavily 429 — assert client backs off and retries.

- **Testing Cache**: Fetch same URL twice — assert second call returns cached response (if caching added).

- **Testing Schema Validation**: Load invalid YAML — assert early validation error with line number.

- **Testing Audit Log**: Enable audit — assert scheduler decisions logged with timestamp, slot, reason.

- **Testing Non-English Refusal**: Feed French/Spanish/Chinese refusals — assert detection (if i18n added).

- **Testing Extensibility**: Add dummy tool to `web_tools`