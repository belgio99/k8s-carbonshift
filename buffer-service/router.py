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

from common import DEFAULT_SCHEDULE, b64dec, b64enc, log, weighted_choice

# ────────────────────────────────────
# Config / env
# ────────────────────────────────────
RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
TRAFFIC_SCHEDULE_NAME: str = os.getenv("TRAFFIC_SCHEDULE_NAME", "default")
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
# Schedule keeper
# ────────────────────────────────────
class TrafficScheduleManager:
    """
    Caches the TrafficSchedule (cluster-scoped CustomResource).
    Responsibilities:
      • initial load
      • continuous watch
      • refresh on expiry (validUntil)
    """

    def __init__(self, name: str) -> None:
        self._name: str = name
        self._current: dict[str, Any] = DEFAULT_SCHEDULE.copy()
        self._lock = asyncio.Lock()

        # Kubernetes client setup
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        self._api = client.CustomObjectsApi()
        self._watch = k8s_watch.Watch()

    # ──────────────── public ────────────────
    async def snapshot(self) -> dict[str, Any]:
        """Returns a thread-safe copy of the current schedule."""
        async with self._lock:
            return self._current.copy()

    # ──────────────── upkeep tasks ────────────────
    async def load_once(self) -> None:
        """Loads the schedule once (on startup or after expiry)."""
        try:
            obj = await asyncio.to_thread(
                self._api.get_cluster_custom_object,
                group="scheduling.carbonshift.io",
                version="v1",
                plural="trafficschedules",
                name=self._name,
            )
            async with self._lock:
                self._current = obj["spec"]
            log.info("Schedule loaded")
        except Exception as exc:  # noqa: BLE001
            log.warning("Schedule load failed: %s", exc)

    async def watch_forever(self) -> None:
        """Continuous stream of events on the CR; updates the cache in real-time."""
        field_selector = f"metadata.name={self._name}"

        while True:
            try:
                stream = self._watch.stream(
                    self._api.list_cluster_custom_object,
                    group="scheduling.carbonshift.io",
                    version="v1",
                    plural="trafficschedules",
                    field_selector=field_selector,
                    timeout_seconds=90,
                )
                for event in stream:
                    async with self._lock:
                        self._current = event["object"]["spec"]
                    log.info("Schedule updated (watch)")
            except Exception as exc:  # noqa: BLE001
                log.error("Watch error: %s", exc)
                await asyncio.sleep(5)

    async def expiry_guard(self) -> None:
        """
        Updates the Prometheus gauge with seconds until `validUntil`
        and forces a reload when it expires.
        """
        while True:
            async with self._lock:
                valid_until = self._current.get("validUntil")

            try:
                expiry_dt = date_parser.isoparse(valid_until)
                seconds_to_expiry = max(
                    (expiry_dt - dt.datetime.utcnow()).total_seconds(),
                    0,
                )
            except Exception:  # noqa: BLE001
                seconds_to_expiry = 60  # fallback

            SCHEDULE_TTL.set(seconds_to_expiry)

            # sleep until expiry, then reload
            await asyncio.sleep(seconds_to_expiry + 1)
            await self.load_once()


# ────────────────────────────────────
# FastAPI router
# ────────────────────────────────────
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
        deadline_sec = schedule["deadlines"].get(f"{flavour}-power", 60)
        ttl_ms = str(int(deadline_sec * 1000))

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

        # Publish the message
        await channel.default_exchange.publish(
            aio_pika.Message(
                json.dumps(payload).encode(),
                correlation_id=correlation_id,
                reply_to=reply_queue.name,
                headers={"flavour": flavour},
                expiration=ttl_ms,
            ),
            routing_key=f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}.{q_type}.{flavour}",
        )
        PUBLISHED_MESSAGES.labels(queue=f"{TARGET_SVC_NAMESPACE}.{TARGET_SVC_NAME}.{q_type}.{flavour}").inc()

        # ─── wait for RPC response ───
        try:
            rabbit_msg = await reply_queue.get(timeout=deadline_sec)
        except asyncio.TimeoutError:
            HTTP_REQUESTS.labels(
                request.method,
                "504",
                q_type,
                flavour,
                bool(forced_flavour),
            ).inc()
            raise HTTPException(status_code=504, detail="Upstream timeout")

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
    schedule_mgr = TrafficScheduleManager(TRAFFIC_SCHEDULE_NAME)

    loop = asyncio.get_event_loop()
    loop.create_task(schedule_mgr.load_once())
    loop.create_task(schedule_mgr.watch_forever())
    loop.create_task(schedule_mgr.expiry_guard())

    uvicorn.run(create_app(schedule_mgr), host="0.0.0.0", port=8000)