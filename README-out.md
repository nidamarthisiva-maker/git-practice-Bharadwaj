# Section D – Observability with OpenTelemetry

**Stack:** Python/Flask app (`orders-api`) → OpenTelemetry SDK → OpenTelemetry Collector → Prometheus (metrics), Tempo (traces), Loki (logs) → Grafana dashboard.
**Deployment environment:** Docker Compose on a single host (laptop / VM). The same images and Collector config work unchanged on Kubernetes, ECS, etc.; only the OTLP endpoint changes.

## Run it
```bash
docker compose up -d --build
./loadgen.sh            # 60 s of healthy traffic
open http://localhost:3000   # Dashboards -> "orders-api – RED + resources"
```

---
## 1. Why observability is required

Once an application is deployed you can no longer attach a debugger; the only way to know what it is doing is what it *tells* you. Observability means emitting enough telemetry (metrics, logs, traces) that you can answer questions you did not anticipate, e.g. "why is checkout slow for some users only?".

| Need | How telemetry helps |
|---|---|
| **Troubleshooting** | Logs give the detailed event ("payment gateway connection refused"); a trace shows where in the request path it happened. |
| **Performance analysis** | Latency histograms (p50/p95/p99) show how slow; span durations show *which step* is slow (DB, downstream call, CPU work). |
| **Reliability** | Error rate, availability and saturation metrics feed SLOs and alerts, so you detect failure before users report it. |
| **Root-cause identification** | Metrics detect *that* something is wrong, traces localise *where*, logs explain *why*. Correlation by `trace_id` ties the three together. |
| **Capacity / cost** | CPU and memory trends show when to scale or where to optimise. |

Monitoring tells you *something* is broken (known failure modes, dashboards/alerts). Observability lets you *investigate* unknown failure modes by slicing high-cardinality data (route, status, trace).

## 2. Instrumentation (OpenTelemetry SDK) – `app/app.py`

* **Resource**: `service.name=orders-api`, version, environment – identifies the source of all signals.
* **Traces**: `TracerProvider` + `BatchSpanProcessor` + OTLP exporter. Flask and `requests` auto-instrumentation create server/client spans and propagate W3C `traceparent`; manual spans (`inventory.check`, `db.query`) add business detail; exceptions are recorded on spans.
* **Metrics**: `MeterProvider` + periodic OTLP reader (5 s). `app.requests` counter, `app.request.duration` histogram (labels: route, method, status_code), CPU % and memory MB gauges.
* **Logs**: Python `logging` bridged with OTel `LoggingHandler`. Records emitted inside a span automatically carry `trace_id`/`span_id`.
* The exporters read `OTEL_EXPORTER_OTLP_ENDPOINT` (set in compose to the Collector) – the app knows nothing about Prometheus/Tempo/Loki, so backends can be swapped without code changes.

## 3. Signals generated

| Signal | Examples |
|---|---|
| Metrics | `app_requests_total`, `app_request_duration_milliseconds_bucket`, `app_process_cpu_percent`, `app_process_memory_mb`, `system_memory_usage_bytes` (Collector hostmetrics) |
| Traces | `GET /api/checkout` → `inventory.check` → `db.query`; `GET` (client) → `GET /payment` (server) |
| Logs | "checkout started", "inventory query is taking unusually long", "payment call failed …" with `trace_id` |

## 4. Collector and backends – `otel-collector-config.yaml`

* **Receivers:** `otlp` (from the app), `hostmetrics` (infra CPU/memory gathered by the Collector).
* **Processors:** `memory_limiter` (protects the Collector), `resource` (adds attributes), `batch` (efficient export).
* **Exporters:** traces → Tempo (OTLP), metrics → Prometheus exporter on :8889 (Prometheus scrapes it), logs → Loki's OTLP endpoint.
* The Collector decouples app from backend: it can retry, buffer, sample, redact or fan-out to several backends.

## 5. Dashboard – "orders-api – RED + resources" (auto-provisioned)

| Panel | Query (PromQL / LogQL) |
|---|---|
| Request count | `sum by (route)(rate(app_requests_total[1m]))` |
| Response latency p50/p95/p99 | `histogram_quantile(0.95, sum by (le)(rate(app_request_duration_milliseconds_bucket[1m])))` |
| Error rate % | `100 * sum(rate(app_requests_total{status_code=~"5.."}[1m])) / sum(rate(app_requests_total[1m]))` |
| Requests by status code | `sum by (status_code)(rate(app_requests_total[1m]))` |
| App CPU / memory | `app_process_cpu_percent`, `app_process_memory_mb` |
| Host memory | `system_memory_usage_bytes{state="used"}` |
| Logs | `{service_name="orders-api"}` |

## 6. How I assess an application after deployment (any environment)

I follow the RED + USE approach and the "four golden signals", then drill from symptom to cause.

| Area | Question | Where I look |
|---|---|---|
| **Availability** | Is it up and reachable? Are health checks passing? | `/health` probe, `up` metric, success ratio (1 – error rate), restarts/crash loops |
| **Errors** | Are requests failing? Which routes/status codes? | Error-rate panel, status-code breakdown, error logs, traces with ERROR status |
| **Latency** | Is it fast enough for the majority *and* the tail? | p50/p95/p99 panel (averages hide tail problems); compare with SLO (e.g. p95 < 500 ms) |
| **Traffic** | Is load normal? Sudden spike or drop? | Request-rate panel; a drop to zero can mean an upstream/routing failure |
| **Resource usage (saturation)** | CPU, memory, disk, connections, threads close to limits? Leaks? | CPU/memory panels, container/node metrics; steadily rising memory = leak |
| **Logs** | What exactly happened? | Filter by level/service/`trace_id` |
| **Traces** | Where is time spent / where does it fail? | Search by duration or error, open waterfall, inspect span attributes/events |
| **Dependencies** | Are DB/cache/downstream APIs healthy and fast? | Client spans (`db.query`, outgoing HTTP) – latency and errors per dependency; service map |

Post-deploy routine: (1) smoke-test and check health; (2) compare golden signals with the previous release/baseline; (3) watch for new error types in logs; (4) check resource trends for 15–30 min under real load; (5) set alerts (error rate > 2 %, p95 > SLO, memory > 85 %) so regressions page someone.

## 7. Demonstration – slow and failed requests

> Run `./loadgen.sh` first to create a healthy baseline, then inject the faults. (The commands below are the exact steps; screenshots can be taken from Grafana at each step.)

### A. Slow request
```bash
./loadgen.sh slow      # 3 requests to /api/checkout?scenario=slow_db  (~2.6 s each)
```
1. **Symptom (metrics):** p95/p99 latency panel jumps from ~100 ms to ~2.5 s; error rate stays ~0 % → slow, not failing.
2. **Locate (traces):** Grafana → Explore → Tempo → search `service.name=orders-api`, *min duration 2 s*. Open the trace: `GET /api/checkout` ≈ 2.6 s, of which `inventory.check` → **`db.query` ≈ 2.5 s**; the payment call is only ~50 ms.
3. **Explain (span attributes + logs):** `db.statement = SELECT * FROM stock WHERE sku=$1 -- no index on sku`. In Loki, `{service_name="orders-api"} | trace_id="<id>"` shows the warning "inventory query is taking unusually long".
4. **Root cause:** slow inventory DB query (missing index), not CPU, memory, or the payment dependency. **Fix:** add an index; verify p95 drops back.

### B. Failed request
```bash
./loadgen.sh fail      # 15 requests to /api/checkout?scenario=payment_down
```
1. **Symptom (metrics):** error-rate panel rises (502s from checkout and 503s from the payment dependency appear in the status-code panel).
2. **Locate (traces):** Tempo → search `status=error`. The trace shows `GET /api/checkout` (ERROR, 502) → client span `GET /payment` → server span `GET /payment` (503, ~1 s). The exception event is recorded on the checkout span.
3. **Explain (logs):** logs for that `trace_id`: "payment gateway connection refused: gateway.internal:443" then "payment call failed … 503".
4. **Root cause:** the downstream payment gateway is unreachable; the application itself is healthy (CPU/memory normal, inventory fast). **Action:** check the gateway/network, add retries/circuit breaker/timeouts, and alert on this dependency.

This is the core workflow: **metrics say something is wrong → traces say where → logs say why.**

## 8. Telemetry flow (application → dashboard)

```
 ┌────────────────────────────┐
 │ orders-api (Flask)         │  OTel SDK: TracerProvider, MeterProvider, LoggerProvider
 │  auto + manual spans       │  Resource: service.name, version, environment
 │  counters/histograms/gauges│
 │  logs (+trace_id)          │
 └─────────────┬──────────────┘
               │ OTLP/gRPC :4317 (batched; metrics every 5 s)
               ▼
 ┌────────────────────────────┐
 │ OpenTelemetry Collector    │  receivers: otlp, hostmetrics
 │                            │  processors: memory_limiter → resource → batch
 └───────┬───────────┬────────┴───────┐
 traces  │   metrics │          logs  │
 OTLP    ▼           ▼ :8889          ▼ OTLP/HTTP
      Tempo      Prometheus ◄─scrape  Loki
         \           │              /
          └──────────┼─────────────┘
                     ▼
                 Grafana  (datasources: Prometheus, Tempo, Loki)
                     └─ dashboard: requests, latency, errors, CPU/memory, logs
```
1. A request hits Flask; the instrumentation starts a span, the app code adds child spans, logs, and records counter/histogram values.
2. SDK processors batch the data in memory; exporters send it over OTLP to the Collector (so the request path is never blocked on a backend).
3. The Collector's pipelines (one per signal) receive, protect (memory limiter), enrich and batch the data, then route each signal to its backend.
4. Prometheus scrapes the Collector every 5 s and stores time series; Tempo stores traces; Loki stores logs (indexed by `service_name`, with `trace_id` as metadata).
5. Grafana queries the three backends (PromQL, TraceQL, LogQL) and renders the dashboard; the shared `trace_id` lets you jump from a slow-latency spike to a trace to its logs.

## Files
`app/` application + Dockerfile · `otel-collector-config.yaml` · `prometheus.yml` · `tempo.yaml` · `docker-compose.yml` · `grafana/` provisioning + dashboard JSON · `loadgen.sh` traffic and fault generator
