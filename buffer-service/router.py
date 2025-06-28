"""
carbonshift_router.py
HTTP → RabbitMQ router with “direct/queue” load balancing based on
CustomResource (TrafficSchedule). Exposes Prometheus metrics.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from typing import Any, Dict

import aio_pika
import httpx
import uvicorn
from dateutil import parser as date_parser
from fastapi import FastAPI, HTTPException, Request, Response
from kubernetes import client, config, watch as k8s_watch
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

from common.utils import DEFAULT_SCHEDULE, b64dec, b64enc, log, weighted_choice, debug
from common.schedule import TrafficScheduleManager

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

# ───────────────────────────────────
# Schedule keeper
# ───────────────────────────────────
# ───────────────────────────────────
# FastAPI router
# ───────────────────────────────────
def create_app(schedule_manager: TrafficScheduleManager) -> FastAPI:
    """
    Builds the FastAPI instance with:
      • /metrics endpoint
      • catch-all proxy that forwards to RabbitMQ
    """

    # Local state for RabbitMQ (reusable connection)
    rabbit_state: dict[str, Any] = {"connection": None, "channel": None}

    async def get_rabbit_channel() -> aio_pika.Channel:
        """Returns a channel (opens one if necessary)."""
        if rabbit_state["channel"] and not rabbit_state["channel"].is_closed:
            return rabbit_state["channel"]

        rabbit_state["connection"] = await aio_pika.connect_robust(RABBITMQ_URL)
        rabbit_state["channel"] = await rabbit_state["connection"].channel()
        return rabbit_state["channel"]

    app = FastAPI(
        title="carbonshift-router",
        docs_url=None,
        redoc_url=None,
    )

    # ───────────── /metrics ─────────────
    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ───────────── catch-all proxy ─────────────
    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(full_path: str, request: Request) -> Response:  # noqa: C901
        """
        Forwards the request to RabbitMQ choosing:
          • direct vs queue
          • flavour
        Waits for the RPC response and forwards it to the HTTP client.
        """

        debug(f"Proxy start: method={request.method} path=/{full_path}")
        schedule = await schedule_manager.snapshot()
        headers: Dict[str, str] = dict(request.headers)

        # ─── select strategy / flavour ───
        urgent = headers.get("X-Urgent", "false").lower() == "true"
        forced_flavour = headers.get("X-Carbonshift")

        q_type = (
            "direct"
            if urgent
            else weighted_choice(
                {"direct": schedule["directWeight"], "queue": schedule["queueWeight"]}
            )
        )
        flavour = forced_flavour or weighted_choice(schedule["flavorWeights"])
        debug(f"Selected routing: q_type={q_type}, flavour={flavour}, forced={bool(forced_flavour)}")
        deadline_sec = schedule["deadlines"].get(f"{flavour}-power", 60)
        expiration_ms = int(deadline_sec * 1000)

        # ─── build payload ───
        payload = {
            "method": request.method,
            "path": f"/{full_path}",
            "query": str(request.query_params),
            "headers": headers,
            "body": b64enc(await request.body()),
        }

        correlation_id = str(uuid.uuid4())
        channel = await get_rabbit_channel()
        reply_queue = await channel.declare_queue(exclusive=True, auto_delete=True)

        response_future: "asyncio.Future[aio_pika.IncomingMessage]" = asyncio.get_running_loop().create_future()

        async def _on_response(msg: aio_pika.IncomingMessage) -> None:
            response_future.set_result(msg)

        consume_tag = await reply_queue.consume(_on_response, no_ack=False)

        # Publish the message
        await channel.default_exchange.publish(
            aio_pika.Message(
                json.dumps(payload).encode(),
                correlation_id=correlation_id,
                reply_to=reply_queue.name,
                headers={"flavour": flavour},
                expiration=expiration_ms,
            ),
            routing_key=f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}.{q_type}.{flavour}",
        )
        PUBLISHED_MESSAGES.labels(queue=f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}.{q_type}.{flavour}").inc()

        # ─── wait for RPC response ───
        try:
            rabbit_msg = await asyncio.wait_for(response_future, timeout=deadline_sec)
        except asyncio.TimeoutError:
            HTTP_REQUESTS.labels(
                request.method,
                "504",
                q_type,
                flavour,
                bool(forced_flavour),
            ).inc()
            raise HTTPException(status_code=504, detail="Upstream timeout")
        finally:
            await reply_queue.cancel(consume_tag)

        with rabbit_msg.process():
            response_data = json.loads(rabbit_msg.body)

        status_code = int(response_data.get("status", 200))
        HTTP_REQUESTS.labels(
            request.method,
            str(status_code),
            q_type,
            flavour,
            bool(forced_flavour),
        ).inc()
        HTTP_LATENCY.labels(q_type, flavour).observe(float(deadline_sec))

        # The "Content-Length" header should be left to FastAPI
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
                "content-type",
                "application/octet-stream",
            ),
        )

    return app


# ────────────────────────────────────
# main
# ────────────────────────────────────
if __name__ == "__main__":
    # Start the Prometheus metrics server
    start_http_server(METRICS_PORT)
    schedule_mgr = TrafficScheduleManager(TS_NAME)

    loop = asyncio.get_event_loop()
    loop.create_task(schedule_mgr.load_once())
    loop.create_task(schedule_mgr.watch_forever())
    loop.create_task(schedule_mgr.expiry_guard())

    uvicorn.run(create_app(schedule_mgr), host="0.0.0.0", port=8000)