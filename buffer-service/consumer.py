#!/usr/bin/env python3
"""
consumer.py
────────────────────────────────────────────────────────────────────────────
Consumes the AMQP queues populated by carbonshift-router, forwards the embedded
HTTP request to the target service and answers via AMQP (RPC style).
"""

# ──────────────────────────────────────────────────────────────
# Faster event-loop (must be done BEFORE importing asyncio)
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import uvloop  # type: ignore
uvloop.install()
l

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any, Dict

import aio_pika
from aio_pika import ExchangeType
from aio_pika.pool import Pool
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

from common.schedule import TrafficScheduleManager
from common.utils import b64dec, b64enc, debug, log

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────
RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

TARGET_SVC_NAMESPACE: str = os.getenv("TARGET_SVC_NAMESPACE", "default").lower()
TARGET_SVC_NAME: str = os.getenv("TARGET_SVC_NAME", "unknown-svc").lower()
TARGET_SVC_SCHEME: str = os.getenv("TARGET_SVC_SCHEME", "http")
TARGET_SVC_PORT: str | None = os.getenv("TARGET_SVC_PORT")

TARGET_BASE_URL: str = (
    f"{TARGET_SVC_SCHEME}://{TARGET_SVC_NAME}.{TARGET_SVC_NAMESPACE}.svc.cluster.local"
    + (f":{TARGET_SVC_PORT}" if TARGET_SVC_PORT else "")
)

TS_NAME: str = os.getenv("TS_NAME", "traffic-schedule")
METRICS_PORT: int = int(os.getenv("METRICS_PORT", "8001"))

FLAVOURS: tuple[str, ...] = ("high-power", "mid-power", "low-power")
QUEUE_PREFIX: str = f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}"
EXCHANGE_NAME: str = QUEUE_PREFIX

# Per-queue concurrency (can be tuned by ENV)
CONCURRENCY: int = int(os.getenv("CONCURRENCY_PER_QUEUE", "32"))

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


# ──────────────────────────────────────────────────────────────
# HTTP forward + AMQP reply
# ──────────────────────────────────────────────────────────────
async def forward_and_reply(
    message: aio_pika.IncomingMessage,
    flavour: str,
    channel_pool: Pool,
    http_client: httpx.AsyncClient,
) -> tuple[int, float]:
    """
    Execute the HTTP request embedded in `message` and publish the response
    to `message.reply_to`. Returns (status_code, elapsed_seconds).
    """
    start_ts = time.perf_counter()

    async with message.process(requeue=True):  # auto-nack on error
        try:
            payload: Dict[str, Any] = json.loads(message.body)

            debug(
                f"Payload: method={payload.get('method')} path={payload.get('path')} headers={payload.get('headers')}"
            )

            response = await http_client.request(
                method=payload["method"],
                url=f"{TARGET_BASE_URL}{payload['path']}",
                params=payload.get("query"),
                headers={**payload.get("headers", {}), "x-carbonshift": flavour},
                content=b64dec(payload["body"]),
            )

            status_code = response.status_code
            response_headers = dict(response.headers)
            response_body = response.content

        except Exception as exc:  # network / decode failure
            status_code = 500
            response_headers = {"content-type": "application/json"}
            response_body = json.dumps({"error": str(exc)}).encode()

        # Publish RPC reply using a pooled channel (avoids single-channel lock)
        async with channel_pool.acquire() as publish_ch:
            await publish_ch.default_exchange.publish(
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
    listen_channel: aio_pika.Channel,
    exchange: aio_pika.Exchange,
    flavour: str,
    channel_pool: Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """
    Consume <prefix>.direct.<flavour> – always active.
    Uses a Semaphore to handle many messages in parallel.
    """
    queue_name = f"{QUEUE_PREFIX}.direct.{flavour}"
    queue = await listen_channel.declare_queue(queue_name, durable=True)

    await queue.bind(
        exchange,
        arguments={"x-match": "all", "q_type": "direct", "flavour": flavour},
    )
    debug(f"Now listening direct: {queue_name}")

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _on_message(message: aio_pika.IncomingMessage) -> None:
        async with sem:
            flav_hdr = message.headers.get("flavour", flavour)
            status, dt_sec = await forward_and_reply(
                message, flav_hdr, channel_pool, http_client
            )
            MSG_CONSUMED.labels("direct", flav_hdr).inc()
            HTTP_FORWARD_COUNT.labels(str(status), flav_hdr).inc()
            HTTP_FORWARD_LAT.labels(flav_hdr).observe(dt_sec)

    await queue.consume(_on_message, no_ack=False)
    await asyncio.Event().wait()  # keep task alive forever


# ──────────────────────────────────────────────────────────────
# Worker – buffer path (queue.*)  pausable via TrafficSchedule
# ──────────────────────────────────────────────────────────────
async def consume_buffer_queue(
    listen_channel: aio_pika.Channel,
    exchange: aio_pika.Exchange,
    flavour: str,
    schedule: TrafficScheduleManager,
    channel_pool: Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """
    Consume <prefix>.queue.<flavour>.
    When buffers are disabled we cancel the consumer (messages stay queued).
    """
    queue_name = f"{QUEUE_PREFIX}.queue.{flavour}"
    queue = await listen_channel.declare_queue(queue_name, durable=True)
    await queue.bind(
        exchange,
        arguments={"x-match": "all", "q_type": "queue", "flavour": flavour},
    )
    debug(f"Queue declared once: {queue_name}")

    consumer_tag: str | None = None
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _on_message(message: aio_pika.IncomingMessage) -> None:
        async with sem:
            # If flag flips mid-stream → requeue and exit early
            if not schedule.consumption_enabled:
                await message.nack(requeue=True)
                return

            flav_hdr = message.headers.get("flavour", flavour)
            status, dt_sec = await forward_and_reply(
                message, flav_hdr, channel_pool, http_client
            )
            MSG_CONSUMED.labels("queue", flav_hdr).inc()
            HTTP_FORWARD_COUNT.labels(str(status), flav_hdr).inc()
            HTTP_FORWARD_LAT.labels(flav_hdr).observe(dt_sec)

    # Loop that toggles the consumer on/off according to schedule
    while True:
        if schedule.consumption_enabled and consumer_tag is None:
            consumer_tag = await queue.consume(_on_message, no_ack=False)
            BUFFER_ENABLED.set(1)
            log.info("Buffer enabled – consuming %s", queue_name)

        elif not schedule.consumption_enabled and consumer_tag is not None:
            await queue.cancel(consumer_tag)
            consumer_tag = None
            BUFFER_ENABLED.set(0)
            log.info("Buffer disabled – paused %s", queue_name)

        # Small sleep to avoid busy-loop
        await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────
# Main bootstrap
# ──────────────────────────────────────────────────────────────
async def main() -> None:
    # Prometheus endpoint
    start_http_server(METRICS_PORT)

    # TrafficSchedule manager (background task)
    schedule_mgr = TrafficScheduleManager(TS_NAME)
    asyncio.create_task(schedule_mgr.load_once())
    asyncio.create_task(schedule_mgr.watch_forever())
    asyncio.create_task(schedule_mgr.expiry_guard())

    # ── AMQP pools ────────────────────────────────────────────
    connection_pool: Pool[aio_pika.RobustConnection] = Pool(
        lambda: aio_pika.connect_robust(RABBITMQ_URL), max_size=2
    )

    async def _get_channel() -> aio_pika.RobustChannel:
        """Return a new channel from the pooled connection."""
        conn = await connection_pool.acquire()
        return await conn.channel()

    channel_pool: Pool[aio_pika.RobustChannel] = Pool(_get_channel, max_size=64)

    # Dedicated connection/channel for consuming
    listen_connection = await aio_pika.connect_robust(RABBITMQ_URL)
    listen_channel = await listen_connection.channel()
    await listen_channel.set_qos(prefetch_count=CONCURRENCY * 2)

    # Headers-exchange
    exchange = await listen_channel.declare_exchange(
        EXCHANGE_NAME, ExchangeType.HEADERS, durable=True
    )

    # Shared HTTP client
    http_client = httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
        timeout=httpx.Timeout(10.0),
    )

    # Spawn workers
    for flav in FLAVOURS:
        asyncio.create_task(
            consume_direct_queue(
                listen_channel, exchange, flav, channel_pool, http_client
            )
        )
        asyncio.create_task(
            consume_buffer_queue(
                listen_channel, exchange, flav, schedule_mgr, channel_pool, http_client
            )
        )

    # FastAPI (only /metrics) – no lifespan
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, lifespan="off", log_level="info")
    asyncio.create_task(uvicorn.Server(config).serve())

    # ── Graceful shutdown ─────────────────────────────────────
    stop_event = asyncio.Event()

    def _stop() -> None:
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    await stop_event.wait()

    await listen_connection.close()
    await http_client.aclose()
    await schedule_mgr.close()


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())