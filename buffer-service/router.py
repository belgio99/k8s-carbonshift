"""
Servizio HTTP sincrono → pubblica su RabbitMQ → attende reply.
Espone anche /metrics per Prometheus.
"""
import os, asyncio, uuid, json, datetime, signal
import aio_pika, httpx, dateutil.parser
from fastapi import FastAPI, Request, Response, HTTPException
from prometheus_client import Counter, Histogram, Gauge, CONTENT_TYPE_LATEST, generate_latest
import uvicorn
from common import b64enc, b64dec, weighted_choice, DEFAULT_SCHEDULE, log


RABBIT_URL       = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
TS_NS            = os.getenv("TS_NAMESPACE", "default")
TS_NAME          = os.getenv("TS_NAME", "current")
METRICS_PORT     = int(os.getenv("METRICS_PORT", "8001"))


REQS  = Counter ("router_http_requests_total",
                 "Total HTTP requests", ["method","status","qtype","flavour","forced"])
LAT   = Histogram("router_request_duration_seconds",
                 "End-to-end latency", ["qtype","flavour"])
PUBL  = Counter ("router_messages_published_total",
                 "Messages published to RabbitMQ", ["queue"])
TTL_G = Gauge   ("router_schedule_valid_seconds",
                 "Seconds before current schedule expires")


class ScheduleKeeper:
    def __init__(self):
        self.current = DEFAULT_SCHEDULE.copy()
        self._lock  = asyncio.Lock()
        # Kubernetes client è opzionale: lo importiamo solo se in cluster
        from kubernetes import client, config, watch
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self._api   = client.CustomObjectsApi()
        self._watch = watch.Watch()

    async def refresh_now(self):
        try:
            obj = await asyncio.to_thread(
                self._api.get_namespaced_custom_object,
                group="carbonshift.io", version="v1",
                namespace=TS_NS, plural="trafficschedules", name=TS_NAME)
            async with self._lock:
                self.current = obj["spec"]
                log.info("Schedule updated (manual fetch)")
        except Exception as e:
            log.warning("Cannot fetch TrafficSchedule: %s", e)

    async def watch_cr(self):
        while True:
            try:
                stream = self._watch.stream(
                    self._api.get_namespaced_custom_object,
                    group="carbonshift.io", version="v1",
                    namespace=TS_NS, plural="trafficschedules",
                    name=TS_NAME, timeout_seconds=90)
                for event in stream:
                    async with self._lock:
                        self.current = event["object"]["spec"]
                        log.info("Schedule updated via watch")
            except Exception as e:
                log.error("watch_cr error: %s", e)
                await asyncio.sleep(5)

    async def valid_until_refresher(self):
        while True:
            async with self._lock:
                vu = self.current.get("validUntil")
            try:
                dt = dateutil.parser.isoparse(vu)
                delta = (dt - datetime.datetime.utcnow()).total_seconds()
            except Exception:
                delta = 60
            TTL_G.set(delta)
            await asyncio.sleep(max(delta, 0)+1)
            await self.refresh_now()

    async def get(self):
        async with self._lock:
            return self.current.copy()

# ─────────── build FastAPI app ───────────
def build_app(sched_keeper: ScheduleKeeper) -> FastAPI:
    rabbit = {"conn": None, "chan": None}

    async def get_chan():
        if rabbit["chan"] and not rabbit["chan"].is_closed:
            return rabbit["chan"]
        rabbit["conn"] = await aio_pika.connect_robust(RABBIT_URL)
        rabbit["chan"] = await rabbit["conn"].channel()
        return rabbit["chan"]

    app = FastAPI(title="carbonshift-router", docs_url=None, redoc_url=None)

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
    async def any_route(path: str, request: Request):
        t0 = datetime.datetime.utcnow()
        sched = await sched_keeper.get()

        headers_in  = dict(request.headers)
        urgent      = headers_in.get("X-Urgent", "false").lower() == "true"
        forced      = headers_in.get("X-Carbonshift")
        qtype       = "direct" if urgent else weighted_choice(
                {"direct": sched["directWeight"], "queue": sched["queueWeight"]})
        flavour     = forced or weighted_choice(sched["flavorWeights"])
        dline       = sched["deadlines"].get(f"{flavour}-power", 60)
        ttl_ms      = str(int(dline)*1000)

        body = await request.body()
        payload = {"method": request.method,
                   "path": "/"+path,
                   "query": str(request.query_params),
                   "headers": headers_in,
                   "body": b64enc(body)}

        corr = str(uuid.uuid4())
        chan = await get_chan()
        reply_q = await chan.declare_queue(exclusive=True, auto_delete=True)

        await chan.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(payload).encode(),
                correlation_id=corr,
                reply_to=reply_q.name,
                headers={"flavour": flavour},
                expiration=ttl_ms),
            routing_key=f"{qtype}.{flavour}")
        PUBL.labels(queue=f"{qtype}.{flavour}").inc()

        try:
            msg = await reply_q.get(timeout=dline)
        except asyncio.TimeoutError:
            REQS.labels(request.method,"504",qtype,flavour,bool(forced)).inc()
            raise HTTPException(504, "Upstream timeout")

        with msg.process():
            data = json.loads(msg.body)
        status = str(data.get("status",200))
        REQS.labels(request.method,status,qtype,flavour,bool(forced)).inc()
        LAT.labels(qtype,flavour).observe((datetime.datetime.utcnow()-t0).total_seconds())
        return Response(b64dec(data["body"]),
                        status_code=int(status),
                        headers={k:v for k,v in data.get("headers",{}).items()
                                 if k.lower() not in ("content-length",)},
                        media_type=data.get("headers",{}).get("content-type",
                                                              "application/octet-stream"))
    return app


if __name__ == "__main__":
    keeper = ScheduleKeeper()
    loop = asyncio.get_event_loop()
    loop.create_task(keeper.watch_cr())
    loop.create_task(keeper.valid_until_refresher())

    app = build_app(keeper)
    uvicorn.run(app, host="0.0.0.0", port=8000)