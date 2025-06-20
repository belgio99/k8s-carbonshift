"""
Worker RabbitMQ → carbonstat → reply.
Disabilita queue.* se consumption_enabled=0 nella TrafficSchedule.
Espone /metrics su HTTP (Prometheus).
"""
import os, asyncio, json, signal, time, datetime
import aio_pika, httpx, dateutil.parser
from prometheus_client import Counter, Histogram, Gauge, CONTENT_TYPE_LATEST, generate_latest
from fastapi import FastAPI, Response
import uvicorn
from common import b64enc, b64dec, log, DEFAULT_SCHEDULE

RABBIT_URL   = os.getenv("RABBITMQ_URL","amqp://guest:guest@rabbitmq:5672/")
CARBON_HOST  = os.getenv("CARBONSTAT_URL","http://carbonstat")
TS_NS        = os.getenv("TS_NAMESPACE","default")
TS_NAME      = os.getenv("TS_NAME","current")
METRICS_PORT = int(os.getenv("METRICS_PORT","8001"))
FLAVOURS     = ("high","mid","low")

CONSUMED = Counter ("consumer_messages_consumed_total",
                    "Messages taken", ["queue","flavour"])
PROC_LAT = Histogram("consumer_processing_duration_seconds",
                     "Duration per msg", ["flavour"])
HTTP_C   = Counter ("consumer_carbonstat_requests_total",
                    "HTTP requests to carbonstat", ["status","flavour"])
ENABLED  = Gauge   ("consumer_queue_enabled", "1 if queue.* enabled else 0")


class EnableKeeper:
    def __init__(self):
        self.enabled = bool(DEFAULT_SCHEDULE["consumption_enabled"])
        from kubernetes import client, config, watch
        try: config.load_incluster_config()
        except Exception: config.load_kube_config()
        self._api   = client.CustomObjectsApi()
        self._watch = watch.Watch()

    async def watch(self):
        while True:
            try:
                stream = self._watch.stream(
                    self._api.get_namespaced_custom_object,
                    group="carbonshift.io", version="v1",
                    namespace=TS_NS, plural="trafficschedules",
                    name=TS_NAME, timeout_seconds=90)
                for ev in stream:
                    self.enabled = bool(ev["object"]["spec"].get("consumption_enabled",1))
                    ENABLED.set(int(self.enabled))
            except Exception as e:
                log.error("enable watch err: %s", e)
                await asyncio.sleep(5)


app = FastAPI(title="consumer-metrics", docs_url=None, redoc_url=None)
@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def worker(loop, channel: aio_pika.Channel, queue: str,
                 keeper: EnableKeeper, client: httpx.AsyncClient):
    q = await channel.declare_queue(queue, durable=True)
    async with q.iterator() as it:
        async for msg in it:
            flavour = msg.headers.get("flavour","mid")
            if queue.startswith("queue.") and not keeper.enabled:
                await asyncio.sleep(1)
                await msg.nack(requeue=True)
                continue
            start = time.perf_counter()
            async with msg.process(requeue=True):
                payload = json.loads(msg.body)
                try:
                    resp = await client.request(
                        payload["method"],
                        f"{CARBON_HOST}{payload['path']}",
                        params=payload["query"],
                        headers={**payload["headers"], "X-Carbonshift": flavour},
                        content=b64dec(payload["body"]))
                    status = resp.status_code
                    body   = resp.content
                    hdrs   = dict(resp.headers)
                except Exception as e:
                    status = 500
                    body   = json.dumps({"error":str(e)}).encode()
                    hdrs   = {"content-type":"application/json"}
                # reply
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps({"status":status,
                                         "headers":hdrs,
                                         "body": b64enc(body)}).encode(),
                        correlation_id=msg.correlation_id),
                    routing_key=msg.reply_to)

            # metrics
            CONSUMED.labels(queue, flavour).inc()
            PROC_LAT.labels(flavour).observe(time.perf_counter()-start)
            HTTP_C.labels(str(status), flavour).inc()

async def main():
    keeper = EnableKeeper()
    loop = asyncio.get_event_loop()
    loop.create_task(keeper.watch())

    conn  = await aio_pika.connect_robust(RABBIT_URL)
    chan  = await conn.channel()
    await chan.set_qos(prefetch_count=10)
    httpc = httpx.AsyncClient()

    for flav in FLAVOURS:
        loop.create_task(worker(loop, chan, f"direct.{flav}", keeper, httpc))
        loop.create_task(worker(loop, chan, f"queue.{flav}",  keeper, httpc))

    # metrics server endpoint
    loop.create_task(uvicorn.run(app, host="0.0.0.0", port=METRICS_PORT, lifespan="off"))

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, lambda: asyncio.create_task(conn.close()))
    await conn.closing

if __name__ == "__main__":
    asyncio.run(main())