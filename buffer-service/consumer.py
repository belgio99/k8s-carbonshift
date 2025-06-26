#!/usr/bin/env python3
"""
RabbitMQ → target svc → reply

This service
1. Listens to RabbitMQ queues (`direct.<flavour>` and `queue.<flavour>`).
2. For every message, performs the HTTP request described in the
   message and sends the response back through RabbitMQ
   (RPC style – `reply_to` + `correlation_id`).
3. If the current TrafficSchedule CRD has `spec.consumption_enabled = 0`
   it pauses the *buffer* queues (`queue.*`) but keeps consuming the
   *direct* queues.
4. Publishes Prometheus metrics on /metrics (FastAPI).

All comments are deliberately left in English for consistency.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import Any, Dict

import aio_pika
import httpx
import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from kubernetes import client as k8s_client, config as k8s_config, watch as k8s_watch

# Local helpers (base64 helpers, logger and DEFAULT_SCHEDULE come from your project)
from common import b64dec, b64enc, log, DEFAULT_SCHEDULE

# ──────────────────────────────────────────────
# Configuration via environment variables
# ──────────────────────────────────────────────
RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
TARGET_SVC_NAME: str = os.getenv("TARGET_SVC_NAME", "http://carbonstat")
TARGET_SVC_NAMESPACE: str = os.getenv("TARGET_SVC_NAMESPACE", "default")
TRAFFIC_SCHEDULE_NAME: str = os.getenv("TS_NAME", "current")
METRICS_PORT: int = int(os.getenv("METRICS_PORT", "8001"))
FLAVOURS: tuple[str, ...] = ("high", "mid", "low")

# ──────────────────────────────────────────────
# Prometheus metrics
# ──────────────────────────────────────────────
MSG_CONSUMED = Counter(
    "consumer_messages_consumed_total",
    "Number of messages consumed",
    ["queue", "flavour"],
)
PROCESSING_LAT = Histogram(
    "consumer_processing_duration_seconds",
    "Time spent processing a single message",
    ["flavour"],
)
HTTP_REQ_FORWARDED = Counter(
    "consumer_requests_total",
    "HTTP requests sent to target service",
    ["status", "flavour"],
)
QUEUE_ENABLED = Gauge(
    "consumer_queue_enabled",
    "1 if queue.* consumption is enabled, 0 otherwise",
)

# ──────────────────────────────────────────────
# FastAPI app that just exposes the metrics
# ──────────────────────────────────────────────
app = FastAPI(title="consumer-metrics", docs_url=None, redoc_url=None)


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ──────────────────────────────────────────────
# Helper: watch TrafficSchedule.consumption_enabled
# ──────────────────────────────────────────────
class ConsumptionSwitch:
    """Keeps track of the `consumption_enabled` flag in TrafficSchedule."""

    def __init__(self) -> None:
        # Initial state comes from the default schedule (config-map)
        self.enabled: bool = bool(DEFAULT_SCHEDULE["consumption_enabled"])

        # Load Kubernetes config (in-cluster or from ~/.kube/config)
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        self._api = k8s_client.CustomObjectsApi()
        self._watch = k8s_watch.Watch()

    async def run(self) -> None:
        """Watch loop – keeps the .enabled property up-to-date."""
        while True:
            try:
                stream = self._watch.stream(
                    self._api.get_cluster_custom_object,
                    group="carbonshift.io",
                    version="v1",
                    plural="trafficschedules",
                    name=TRAFFIC_SCHEDULE_NAME,
                    timeout_seconds=90,
                )
                for event in stream:
                    spec = event["object"]["spec"]
                    self.enabled = bool(spec.get("consumption_enabled", 1))
                    QUEUE_ENABLED.set(int(self.enabled))
            except Exception as exc:
                log.error("TrafficSchedule watch error: %s", exc)
                await asyncio.sleep(5)


# ──────────────────────────────────────────────
# Worker coroutine: consumes a single AMQP queue
# ──────────────────────────────────────────────
async def worker(
    amqp_channel: aio_pika.Channel,
    queue_name: str,
    switch: ConsumptionSwitch,
    http_client: httpx.AsyncClient,
) -> None:
    """Consume an AMQP queue and forward messages to target service."""
    queue = await amqp_channel.declare_queue(queue_name, durable=True)

    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            flavour: str = message.headers.get("flavour", "mid")

            # If it's a buffer queue (queue.*) and disabled, immediately requeue
            if queue_name.startswith("queue.") and not switch.enabled:
                await asyncio.sleep(1)  # tiny back-off
                await message.nack(requeue=True)
                continue

            start_ts = time.perf_counter()
            async with message.process(requeue=True):
                # ----------------------------------------------------------------
                # 1. Perform the HTTP request described in the message
                # ----------------------------------------------------------------
                try:
                    payload: Dict[str, Any] = json.loads(message.body)

                    response = await http_client.request(
                        method=payload["method"],
                        url=f"{TARGET_SVC_NAME}{payload['path']}",
                        params=payload.get("query"),
                        headers={
                            **payload.get("headers", {}),
                            "X-Carbonshift": flavour,
                        },
                        content=b64dec(payload["body"]),
                    )

                    status_code = response.status_code
                    response_body = response.content
                    response_headers = dict(response.headers)

                except Exception as exc:
                    # Network or validation error → synthesize 500 response
                    status_code = 500
                    response_body = json.dumps({"error": str(exc)}).encode()
                    response_headers = {"content-type": "application/json"}

                # ----------------------------------------------------------------
                # 2. Send the reply back to the requester via AMQP
                # ----------------------------------------------------------------
                await amqp_channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(
                            {
                                "status": status_code,
                                "headers": response_headers,
                                "body": b64enc(response_body),
                            }
                        ).encode(),
                        correlation_id=message.correlation_id,
                    ),
                    routing_key=message.reply_to,
                )

            # --------------------------------------------------------------------
            # 3. Update metrics
            # --------------------------------------------------------------------
            MSG_CONSUMED.labels(queue_name, flavour).inc()
            PROCESSING_LAT.labels(flavour).observe(time.perf_counter() - start_ts)
            HTTP_REQ_FORWARDED.labels(str(status_code), flavour).inc()


# ──────────────────────────────────────────────
# Main entry-point
# ──────────────────────────────────────────────
async def main() -> None:
    # Start the Prometheus metrics server
    start_http_server(METRICS_PORT)
    switch = ConsumptionSwitch()
    loop = asyncio.get_event_loop()

    # Background: watch TrafficSchedule
    loop.create_task(switch.run())

    # AMQP connection + channel
    amqp_connection = await aio_pika.connect_robust(RABBITMQ_URL)
    amqp_channel = await amqp_connection.channel()
    await amqp_channel.set_qos(prefetch_count=10)

    # Shared HTTP client
    http_client = httpx.AsyncClient()

    # Spawn one worker per queue flavour (direct.* and queue.*)
    for flavour in FLAVOURS:
        loop.create_task(
            worker(amqp_channel, f"direct.{flavour}", switch, http_client)
        )
        loop.create_task(
            worker(amqp_channel, f"queue.{flavour}", switch, http_client)
        )

    # Expose /metrics
    loop.create_task(
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=METRICS_PORT,
            lifespan="off",
            log_level="info",
        )
    )

    # Graceful shutdown on SIGINT / SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(amqp_connection.close()))

    await amqp_connection.closing  # wait until connection is fully closed


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())