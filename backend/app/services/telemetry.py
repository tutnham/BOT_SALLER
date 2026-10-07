"""Prometheus metrics with bounded labels. Process-local counters/histograms."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_registry: CollectorRegistry | None = None
_bundle: _MetricBundle | None = None


class _MetricBundle:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.webhook_total = Counter(
            "zakupki_webhook_total",
            "Webhook HTTP requests accepted by this process",
            registry=registry,
        )
        self.webhook_errors = Counter(
            "zakupki_webhook_errors",
            "Webhook processing or enqueue failures in this process",
            registry=registry,
        )
        self.http_duration = Histogram(
            "zakupki_http_client_duration_seconds",
            "Outbound HTTP client call duration",
            labelnames=("operation", "outcome"),
            registry=registry,
        )
        self.llm_duration = Histogram(
            "zakupki_llm_duration_seconds",
            "LLM call duration",
            labelnames=("operation", "outcome"),
            registry=registry,
        )
        self.cron_duration = Histogram(
            "zakupki_cron_job_duration_seconds",
            "In-process cron job duration",
            labelnames=("job", "outcome"),
            registry=registry,
        )
        self.parser_errors = Counter(
            "zakupki_parser_client_errors_total",
            "Parser API client failures in this process",
            registry=registry,
        )
        self.inbox_pending = Gauge(
            "zakupki_inbox_pending",
            "Unresolved webhook inbox rows in pending",
            registry=registry,
        )
        self.inbox_processing = Gauge(
            "zakupki_inbox_processing",
            "Webhook inbox rows in processing",
            registry=registry,
        )
        self.inbox_dead = Gauge(
            "zakupki_inbox_dead",
            "Open dead webhook inbox rows",
            registry=registry,
        )
        self.outbox_pending = Gauge(
            "zakupki_outbox_pending",
            "Telegram outbox rows in pending",
            registry=registry,
        )
        self.outbox_processing = Gauge(
            "zakupki_outbox_processing",
            "Telegram outbox rows in processing",
            registry=registry,
        )
        self.outbox_uncertain = Gauge(
            "zakupki_outbox_uncertain",
            "Open uncertain telegram outbox rows",
            registry=registry,
        )
        self.inbox_oldest_pending_seconds = Gauge(
            "zakupki_inbox_oldest_pending_seconds",
            "Age of oldest pending webhook inbox row",
            registry=registry,
        )
        self.worker_heartbeat_age_seconds = Gauge(
            "zakupki_worker_heartbeat_age_seconds",
            "Seconds since latest worker heartbeat (-1 if missing)",
            registry=registry,
        )
        self.scheduler_heartbeat_age_seconds = Gauge(
            "zakupki_scheduler_heartbeat_age_seconds",
            "Seconds since latest scheduler heartbeat (-1 if missing)",
            registry=registry,
        )
        self.morning_price_last_success_unixtime = Gauge(
            "zakupki_morning_price_last_success_unixtime",
            "Unix time of last successful morning-price job run",
            registry=registry,
        )
        self.supplier_binding_total = Counter(
            "supplier_binding_total",
            "Supplier inbound binding decisions",
            labelnames=("method",),
            registry=registry,
        )
        self.supplier_binding_pending_total = Counter(
            "supplier_binding_pending_total",
            "Ambiguous supplier prices entering pending bind sessions",
            registry=registry,
        )
        self.supplier_binding_resolution_seconds = Histogram(
            "supplier_binding_resolution_seconds",
            "Time from pending session to resolution",
            registry=registry,
        )
        self.supplier_binding_prompt_expired_total = Counter(
            "supplier_binding_prompt_expired_total",
            "Expired supplier bind sessions",
            registry=registry,
        )
        self.supplier_binding_llm_abstain_total = Counter(
            "supplier_binding_llm_abstain_total",
            "LLM classify_supplier_reply abstentions for binding",
            registry=registry,
        )
        self.supplier_binding_rebind_total = Counter(
            "supplier_binding_rebind_total",
            "Supplier rebind resolutions",
            labelnames=("original_method",),
            registry=registry,
        )


def get_registry() -> CollectorRegistry:
    global _registry
    if _registry is None:
        _registry = CollectorRegistry()
    return _registry


def get_metrics() -> _MetricBundle:
    global _bundle
    if _bundle is None:
        _bundle = _MetricBundle(get_registry())
    return _bundle


def configure_registry(registry: CollectorRegistry) -> None:
    """Tests: isolated registry without polluting the global default."""
    global _registry, _bundle
    _registry = registry
    _bundle = _MetricBundle(registry)


def record_webhook_accepted() -> None:
    get_metrics().webhook_total.inc()


def record_webhook_error() -> None:
    get_metrics().webhook_errors.inc()


def record_parser_error() -> None:
    get_metrics().parser_errors.inc()


@contextmanager
def observe_http(operation: str) -> Iterator[None]:
    metrics = get_metrics()
    start = time.perf_counter()
    outcome = "success"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        metrics.http_duration.labels(operation=operation, outcome=outcome).observe(
            time.perf_counter() - start
        )


@asynccontextmanager
async def observe_http_async(operation: str) -> AsyncIterator[None]:
    metrics = get_metrics()
    start = time.perf_counter()
    outcome = "success"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        metrics.http_duration.labels(operation=operation, outcome=outcome).observe(
            time.perf_counter() - start
        )


@asynccontextmanager
async def observe_llm(operation: str) -> AsyncIterator[None]:
    metrics = get_metrics()
    start = time.perf_counter()
    outcome = "success"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        metrics.llm_duration.labels(operation=operation, outcome=outcome).observe(
            time.perf_counter() - start
        )


@contextmanager
def observe_cron(job: str) -> Iterator[None]:
    metrics = get_metrics()
    start = time.perf_counter()
    outcome = "success"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        metrics.cron_duration.labels(job=job, outcome=outcome).observe(
            time.perf_counter() - start
        )


def record_supplier_binding_decision(method: str) -> None:
    get_metrics().supplier_binding_total.labels(method=method).inc()


def record_supplier_binding_pending() -> None:
    get_metrics().supplier_binding_pending_total.inc()


def record_supplier_binding_llm_abstain() -> None:
    get_metrics().supplier_binding_llm_abstain_total.inc()


def record_supplier_binding_expired() -> None:
    get_metrics().supplier_binding_prompt_expired_total.inc()


def record_supplier_binding_rebind(original_method: str) -> None:
    get_metrics().supplier_binding_rebind_total.labels(original_method=original_method).inc()


@contextmanager
def observe_supplier_binding_resolution() -> Iterator[None]:
    metrics = get_metrics()
    start = time.perf_counter()
    try:
        yield
    finally:
        metrics.supplier_binding_resolution_seconds.observe(time.perf_counter() - start)
