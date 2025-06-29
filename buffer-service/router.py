#!/usr/bin/env python3
"""
carbonshift_router.py
────────────────────────────────────────────────────────────────────────────
HTTP → RabbitMQ router with “direct/queue” load balancing based on a
CustomResource (TrafficSchedule).  Espone metriche Prometheus.

REV-notes (2024-06)
  • Usa un *headers-exchange* invece della routing-key “<ns>.<svc>.…”.
  • Una sola reply-queue condivisa; le risposte vengono demultiplexate
    tramite `correlation_id` → Future (niente più declare/cancel per req).
  • Tutto il resto è invariato.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from typing import Any, Dict

import aio_pika
from aio_pika import ExchangeType
import uvicorn
from dateutil import parser as date_parser
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

from common.utils import DEFAULT_SCHEDULE, b64dec, b64enc, debug, weighted_choice
from common.schedule import TrafficScheduleManager

# ────────────────────────────────────
# Config
# ────────────────────────────────────
RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
TS_NAME: str = os.getenv("TS_NAME", "traffic-schedule")
METRICS_PORT: int = int(os.getenv("METRICS_PORT", "8001"))
TARGET_SVC_NAME: str = os.getenv("TARGET_SVC_NAME", "unknown-svc").lower()
TARGET_SVC_NAMESPACE: str = os.getenv("TARGET_SVC_NAMESPACE", "default").lower()

# ────────────────────────────────────
# Prometheus metrics
# ────────────────────────────────────
HTTP_REQUESTS = Counter(
    "router_http_requests_total",
    "HTTP requests",
    ["method", "status", "qtype", "flavour", "forced"],
)

HTTP_LATENCY = Histogram(
    "router_request_duration_seconds",
    "End-to-end latency",
    ["qtype", "flavour"],
)

PUBLISHED_MESSAGES = Counter(
    "router_messages_published_total",
    "Messages published",
    ["queue"],
)

SCHEDULE_TTL = Gauge(
    "router_schedule_valid_seconds",
    "Seconds until schedule expiry",
)

# ────────────────────────────────────
# RabbitMQ state  (connection reused)
# ────────────────────────────────────
rabbit_state: dict[str, Any] = {
    "connection": None,
    "channel": None,
    "exchange": None,  # headers-exchange
    "reply_queue": None,  # una sola reply-queue
    "pending": {},  # correlation_id → Future
}


async def _init_rabbit() -> None:
    """
    Opens connection/channel, declares headers-exchange and the single reply
    queue (idempotente: viene chiamato la prima volta che serve un canale).
    """
    if rabbit_state["channel"] and not rabbit_state["channel"].is_closed:
        return  # già inizializzato

    # Connessione e channel robust
    rabbit_state["connection"] = await aio_pika.connect_robust(RABBITMQ_URL)
    rabbit_state["channel"] = await rabbit_state["connection"].channel()

    # Headers-exchange per la pubblicazione
    rabbit_state["exchange"] = await rabbit_state["channel"].declare_exchange(
        f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}",
        ExchangeType.HEADERS,
        durable=True,
    )

    # Reply-queue “privata” del router
    rabbit_state["reply_queue"] = await rabbit_state["channel"].declare_queue(
        exclusive=True, auto_delete=True
    )

    # Consumer fisso che demultiplexa via correlation_id
    async def _on_reply(msg: aio_pika.IncomingMessage) -> None:
        future: asyncio.Future | None = rabbit_state["pending"].pop(
            msg.correlation_id, None
        )
        if future and not future.done():
            future.set_result(msg)

    await rabbit_state["reply_queue"].consume(_on_reply, no_ack=False)


async def get_rabbit_channel() -> aio_pika.Channel:
    """Restituisce il canale AMQP già inizializzato."""
    await _init_rabbit()
    return rabbit_state["channel"]


# ────────────────────────────────────
# FastAPI router
# ────────────────────────────────────
def create_app(schedule_manager: TrafficScheduleManager) -> FastAPI:
    """
    Builds the FastAPI instance with:
      • /metrics endpoint
      • catch-all proxy that forwards to RabbitMQ
    """
    app = FastAPI(title="carbonshift-router", docs_url=None, redoc_url=None)

    # ───────────── catch-all proxy ─────────────
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy(full_path: str, request: Request) -> Response:  # noqa: C901
        debug(f"Proxy start: method={request.method} path=/{full_path}")
        schedule = await schedule_manager.snapshot()

        # ─── select strategy / flavour ───
        urgent = request.headers.get("x-urgent", "false").lower() == "true"
        forced_flavour = request.headers.get("x-carbonshift")

        flavour_weights = {
            r["flavourName"]: r["weight"] for r in schedule.get("flavourRules", [])
        }
        flavour_deadlines = {
            r["flavourName"]: r.get("deadlineSec", 60)
            for r in schedule.get("flavourRules", [])
        }

        headers: Dict[str, str] = dict(request.headers)

        q_type = (
            "direct"
            if urgent
            else weighted_choice(
                {"direct": schedule["directWeight"], "queue": schedule["queueWeight"]}
            )
        )

        flavour = forced_flavour or weighted_choice(flavour_weights)
        debug(
            f"Selected routing: q_type={q_type}, flavour={flavour}, forced={bool(forced_flavour)}"
        )
        deadline_sec = flavour_deadlines.get(flavour, 60)
        expiration_ms = int(deadline_sec * 1000)

        # ─── build payload ───
        payload = {
            "method": request.method,
            "path": f"/{full_path}",
            "query": str(request.query_params),
            "headers": headers,
            "body": b64enc(await request.body()),
        }

        # ─── publish ───
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        response_future: "asyncio.Future[aio_pika.IncomingMessage]" = (
            loop.create_future()
        )
        rabbit_state["pending"][correlation_id] = response_future

        channel = await get_rabbit_channel()
        exchange: aio_pika.Exchange = rabbit_state["exchange"]

        await exchange.publish(
            aio_pika.Message(
                json.dumps(payload).encode(),
                correlation_id=correlation_id,
                reply_to=rabbit_state["reply_queue"].name,
                headers={
                    "q_type": q_type,
                    "flavour": flavour,
                    "namespace": TARGET_SVC_NAMESPACE,
                    "service": TARGET_SVC_NAME,
                },
                expiration=expiration_ms,
            ),
            routing_key="",  # ignorato dal headers-exchange
            mandatory=True,
        )
        PUBLISHED_MESSAGES.labels(
            queue=f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}.{q_type}.{flavour}"
        ).inc()
        debug(
            "Published message: "
            f"headers={{q_type:{q_type}, flavour:{flavour}}}, "
            f"correlation_id={correlation_id}, expiration_ms={expiration_ms}"
        )

        # ─── wait for RPC response ───
        try:
            rabbit_msg = await asyncio.wait_for(response_future, timeout=deadline_sec)
        except asyncio.TimeoutError:
            rabbit_state["pending"].pop(correlation_id, None)
            HTTP_REQUESTS.labels(
                request.method, "504", q_type, flavour, bool(forced_flavour)
            ).inc()
            raise HTTPException(status_code=504, detail="Upstream timeout")

        async with rabbit_msg.process():  # ack al termine
            response_data = json.loads(rabbit_msg.body)

        status_code = int(response_data.get("status", 200))
        HTTP_REQUESTS.labels(
            request.method, str(status_code), q_type, flavour, bool(forced_flavour)
        ).inc()
        HTTP_LATENCY.labels(q_type, flavour).observe(float(deadline_sec))

        response_headers = {
            k: v
            for k, v in response_data.get("headers", {}).items()
            if k.lower() != "content-length"
        }

        return Response(
            b64dec(response_data["body"]),
            status_code=status_code,
            headers=response_headers,
            media_type=response_data.get("headers", {}).get(
                "content-type", "application/octet-stream"
            ),
        )

    return app


# ────────────────────────────────────
# Main
# ────────────────────────────────────
async def main() -> None:
    # Prometheus
    start_http_server(METRICS_PORT)

    schedule_mgr = TrafficScheduleManager(TS_NAME)

    loop = asyncio.get_running_loop()
    loop.create_task(schedule_mgr.load_once())
    loop.create_task(schedule_mgr.watch_forever())
    loop.create_task(schedule_mgr.expiry_guard())

    app = create_app(schedule_mgr)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8000, lifespan="off")
    )
    loop.create_task(server.serve())

    # graceful-shutdown
    stop_event = asyncio.Event()

    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    await stop_event.wait()
    await schedule_mgr.close()
    # chiude anche il rabbit se presente
    if rabbit_state.get("connection"):
        await rabbit_state["connection"].close()


if __name__ == "__main__":
    asyncio.run(main())
