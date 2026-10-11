"""orders-api: a tiny Flask service instrumented with the OpenTelemetry SDK.

Signals produced
  Traces  : Flask + requests auto-instrumentation, plus manual spans
  Metrics : app.requests (counter), app.request.duration (histogram),
            app.process.cpu_percent / app.process.memory_mb (gauges)
  Logs    : Python logging bridged to OTLP; records carry trace_id/span_id
Everything is exported over OTLP/gRPC to the Collector (endpoint comes from
OTEL_EXPORTER_OTLP_ENDPOINT).
"""
import logging
import os
import random
import time

import psutil
import requests
from flask import Flask, g, jsonify, request
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.metrics import Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

# ---------------------------------------------------------------- SDK setup
resource = Resource.create({
    "service.name": "orders-api",
    "service.version": "1.0.0",
    "deployment.environment": os.getenv("DEPLOY_ENV", "demo"),
})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5000)],
)
metrics.set_meter_provider(meter_provider)

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
log = logging.getLogger("orders-api")

tracer = trace.get_tracer("orders-api")
meter = metrics.get_meter("orders-api")

request_counter = meter.create_counter("app.requests", description="HTTP requests handled")
latency_hist = meter.create_histogram("app.request.duration", unit="ms", description="HTTP request latency")

_proc = psutil.Process()
_proc.cpu_percent(interval=None)  # prime the counter

meter.create_observable_gauge(
    "app.process.cpu_percent",
    callbacks=[lambda _o: [Observation(_proc.cpu_percent(interval=None))]],
    description="CPU used by this process (%)",
)
meter.create_observable_gauge(
    "app.process.memory_mb",
    callbacks=[lambda _o: [Observation(_proc.memory_info().rss / 1024 / 1024)]],
    description="Resident memory of this process (MB)",
)

# --------------------------------------------------------------- Flask app
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)   # server spans
RequestsInstrumentor().instrument()       # client spans + trace-context propagation

SELF = "http://localhost:5000"


@app.before_request
def _start_timer():
    g.t0 = time.perf_counter()


@app.after_request
def _record_metrics(resp):
    if request.path != "/health":
        attrs = {
            "route": request.url_rule.rule if request.url_rule else "unmatched",
            "method": request.method,
            "status_code": str(resp.status_code),
        }
        request_counter.add(1, attrs)
        latency_hist.record((time.perf_counter() - g.t0) * 1000, attrs)
    return resp


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/api/orders")
def orders():
    time.sleep(random.uniform(0.01, 0.05))
    log.info("listed orders")
    return jsonify(orders=[{"id": i} for i in range(3)])


@app.route("/api/checkout")
def checkout():
    """scenario = normal | slow_db | payment_down"""
    scenario = request.args.get("scenario", "normal")
    log.info("checkout started scenario=%s", scenario)

    with tracer.start_as_current_span("inventory.check"):
        if scenario == "slow_db":
            with tracer.start_as_current_span("db.query") as db:
                db.set_attribute("db.system", "postgresql")
                db.set_attribute("db.statement", "SELECT * FROM stock WHERE sku = $1  -- no index on sku")
                log.warning("inventory query is taking unusually long")
                time.sleep(2.5)
        else:
            time.sleep(random.uniform(0.02, 0.08))

    try:
        r = requests.get(f"{SELF}/payment", params={"scenario": scenario}, timeout=1.5)
        r.raise_for_status()
    except Exception as exc:
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        log.error("payment call failed: %s", exc)
        return jsonify(error="payment unavailable"), 502

    log.info("checkout completed")
    return jsonify(status="paid")


@app.route("/payment")
def payment():
    """Stands in for a downstream dependency (payment gateway)."""
    if request.args.get("scenario") == "payment_down":
        time.sleep(1.0)
        log.error("payment gateway connection refused: gateway.internal:443")
        return jsonify(error="gateway unreachable"), 503
    time.sleep(random.uniform(0.02, 0.06))
    return jsonify(status="charged")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
