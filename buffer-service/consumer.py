#!/usr/bin/env python3
"""
consumer.py
────────────────────────────────────────────────────────────────────────────
Consumes the AMQP queues populated by carbonshift-router, forwards the embedded
HTTP request to the target service and answers via AMQP (RPC style).

Queue layout created by the router:
    <namespace>.<service>.direct.<flavour>   – real-time path
    <namespace>.<service>.queue.<flavour>    – buffer path (may be paused)

TrafficSchedule contract (immutable):
    • A schedule is never patched; when it “expires” (validUntil) a brand new
      object replaces it.
    • Field `status.consumption_enabled` (0/1) decides whether *buffer* queues
      must be processed.

This consumer therefore:
    1. Loads the schedule once per validity window.
    2. If buffers are disabled, sleeps until `validUntil`.
    3. Reloads the next schedule and repeats.
    4. Keeps consuming real-time queues all the time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import signal
import time
from typing import Any, Dict

import aio_pika
import httpx
import uvicorn
from dateutil import parser as date_parser
from fastapi import FastAPI, Response
from kubernetes import client as k8s_client, config as k8s_config
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

from common.utils import b64dec, b64enc, log, DEFAULT_SCHEDULE
from common.schedule import TrafficScheduleManager

# ──────────────────────────────────────────────────────────────
# Configuration (env-vars with sane defaults)
# ──────────────────────────────────────────────────────────────
RABBITMQ_URL: str = os.getenv(
    "RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/"
)

TARGET_SVC_NAMESPACE: str = os.getenv("TARGET_SVC_NAMESPACE", "default").lower()
TARGET_SVC_NAME: str = os.getenv("TARGET_SVC_NAME", "unknown-svc").lower()
TARGET_SVC_SCHEME: str = os.getenv("TARGET_SVC_SCHEME", "http")
TARGET_SVC_PORT: str | None = os.getenv("TARGET_SVC_PORT")

# e.g. http://unknown-svc.default[:port]
TARGET_BASE_URL: str = (
    f"{TARGET_SVC_SCHEME}://{TARGET_SVC_NAME}.{TARGET_SVC_NAMESPACE}"
    + (f":{TARGET_SVC_PORT}" if TARGET_SVC_PORT else "")
)

TS_NAME: str = os.getenv("TS_NAME", "traffic-schedule")
METRICS_PORT: int = int(os.getenv("METRICS_PORT", "8001"))

FLAVOURS: tuple[str, ...] = ("high", "mid", "low")
QUEUE_PREFIX: str = f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}"

# ──────────────────────────────────────────────────────────────
# Prometheus metrics
# ──────────────────────────────────────────────────────────────
MSG_CONSUMED = Counter(
    "consumer_messages_total",
    "AMQP messages consumed",
    ["queue_type", "flavour"],
)
HTTP_FORWARD_LAT = Histogram(
    "consumer_forward_seconds",
    "Time spent forwarding the HTTP request",
    ["flavour"],
)
HTTP_FORWARD_COUNT = Counter(
    "consumer_http_requests_total",
    "Requests sent to the target service",
    ["status", "flavour"],
)
BUFFER_ENABLED = Gauge(
    "consumer_buffer_enabled",
    "1 when queue.* consumption is enabled, 0 otherwise",
)

# ──────────────────────────────────────────────────────────────
# FastAPI – only /metrics
# ──────────────────────────────────────────────────────────────
app = FastAPI(title="carbonshift-consumer", docs_url=None, redoc_url=None)

@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# ──────────────────────────────────────────────────────────────
# Core helper – forward HTTP + reply over AMQP
# ──────────────────────────────────────────────────────────────
async def forward_and_reply(
    message: aio_pika.IncomingMessage,
    flavour: str,
    channel: aio_pika.Channel,
    http_client: httpx.AsyncClient,
) -> tuple[int, float]:
    """
    Execute the HTTP request embedded in `message` and publish the response
    on `message.reply_to`. Returns (status_code, elapsed_seconds).
    """
    start_ts = time.perf_counter()

    async with message.process(requeue=True):          # auto-nack on error
        try:
            payload: Dict[str, Any] = json.loads(message.body)

            response = await http_client.request(
                method=payload["method"],
                url=f"{TARGET_BASE_URL}{payload['path']}",
                params=payload.get("query"),         # router sends str "a=1&b=2"
                headers={
                    **payload.get("headers", {}),
                    "X-Carbonshift": flavour,
                },
                content=b64dec(payload["body"]),
            )

            status_code = response.status_code
            response_headers = dict(response.headers)
            response_body = response.content

        except Exception as exc:                      # network / decode failure
            status_code = 500
            response_headers = {"content-type": "application/json"}
            response_body = json.dumps({"error": str(exc)}).encode()

        # Publish RPC reply
        await channel.default_exchange.publish(
            aio_pika.Message(
                json.dumps(
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

    elapsed = time.perf_counter() - start_ts
    return status_code, elapsed

# ──────────────────────────────────────────────────────────────
# Worker – real-time path (direct.*)
# ──────────────────────────────────────────────────────────────
async def consume_direct_queue(
    channel: aio_pika.Channel,
    queue_name: str,
    http_client: httpx.AsyncClient,
) -> None:
    """
    Consume <prefix>.direct.<flavour> – always active.
    """
    queue = await channel.declare_queue(queue_name, durable=True)

    async with queue.iterator() as iterator:
        async for message in iterator:
            flavour = message.headers.get("flavour", "mid")
            status, dt_sec = await forward_and_reply(
                message, flavour, channel, http_client
            )

            MSG_CONSUMED.labels("direct", flavour).inc()
            HTTP_FORWARD_COUNT.labels(str(status), flavour).inc()
            HTTP_FORWARD_LAT.labels(flavour).observe(dt_sec)

# ──────────────────────────────────────────────────────────────
# Worker – buffer path (queue.*)  pausable via TrafficSchedule
# ──────────────────────────────────────────────────────────────
async def consume_buffer_queue(
    channel: aio_pika.Channel,
    queue_name: str,
    schedule: TrafficScheduleManager,
    http_client: httpx.AsyncClient,
) -> None:
    """
    Consume <prefix>.queue.<flavour>.
    If buffers are disabled, sleep until the current schedule expires.
    """
    while True:
        # Pause wholesale while disabled
        if not schedule.consumption_enabled:
            sleep_for = schedule.seconds_to_expiry() + 1
            log.info("Buffers disabled – sleeping %.0fs", sleep_for)
            await asyncio.sleep(sleep_for)
            # schedule.refresh_forever() will reload by then
            continue

        # Buffer consumption is enabled → declare consumer
        queue = await channel.declare_queue(queue_name, durable=True)
        async with queue.iterator() as iterator:
            async for message in iterator:
                # If flag flips mid-stream → cancel and restart outer loop
                if not schedule.consumption_enabled:
                    await iterator.close()
                    break

                flavour = message.headers.get("flavour", "mid")
                status, dt_sec = await forward_and_reply(
                    message, flavour, channel, http_client
                )

                MSG_CONSUMED.labels("queue", flavour).inc()
                HTTP_FORWARD_COUNT.labels(str(status), flavour).inc()
                HTTP_FORWARD_LAT.labels(flavour).observe(dt_sec)

# ──────────────────────────────────────────────────────────────
# Main bootstrap
# ──────────────────────────────────────────────────────────────
async def main() -> None:
    # Start the Prometheus metrics server
    start_http_server(METRICS_PORT)
    # TrafficSchedule manager (background task)
    schedule_mgr = TrafficScheduleManager(TS_NAME)

    loop = asyncio.get_event_loop()
    loop.create_task(schedule_mgr.load_once())
    loop.create_task(schedule_mgr.watch_forever())
    loop.create_task(schedule_mgr.expiry_guard())

    # AMQP connection / channel
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=20)

    # Shared HTTP client
    http_client = httpx.AsyncClient()

    # Spawn one worker per (queue type, flavour)
    for flavour in FLAVOURS:
        # Realtime path
        asyncio.create_task(
            consume_direct_queue(
                channel, f"{QUEUE_PREFIX}.direct.{flavour}", http_client
            )
        )
        # Buffer path
        asyncio.create_task(
            consume_buffer_queue(
                channel,
                f"{QUEUE_PREFIX}.queue.{flavour}",
                schedule,
                http_client,
            )
        )

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        lifespan="off",
        log_level="info",
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(connection.close()))

    await connection.wait_closed()

# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":          # pragma: no cover
    asyncio.run(main())