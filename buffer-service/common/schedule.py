"""
TrafficScheduleManager  – shared by router and consumer.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from dateutil import parser as date_parser
from kubernetes_asyncio import client as k8s_client, config as k8s_config, watch as k8s_watch

from .utils import DEFAULT_SCHEDULE, log

__all__ = ["TrafficScheduleManager"]


class TrafficScheduleManager:
    """
    Keeps a cached copy of the cluster-wide TrafficSchedule.
    Exposes:
      • snapshot()          – async read
      • load_once()         – initial load / manual reload
      • watch_forever()     – CRD watch loop
      • expiry_guard()      – reload when validUntil is reached
    """

    def __init__(self, name: str) -> None:
        self._name: str = name
        self._current: dict[str, Any] = DEFAULT_SCHEDULE.copy()
        self._lock = asyncio.Lock()

        # K8s client bootstrap
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        self._api = k8s_client.CustomObjectsApi()
        self._watch = k8s_watch.Watch()

    async def snapshot(self) -> dict[str, Any]:
        """Return a thread-safe clone of the cached schedule."""
        async with self._lock:
            return self._current.copy()

    async def load_once(self) -> None:
        obj = await self._api.get_cluster_custom_object(
            group="scheduling.carbonshift.io",
            version="v1alpha1",
            plural="trafficschedules",
            name=self._name,
        )
        async with self._lock:
            self._current = obj.get("status", {})
        log.info("TrafficSchedule loaded")

    async def watch_forever(self) -> None:
        field_selector = f"metadata.name={self._name}"
        while True:
            try:
                stream = self._watch.stream(
                    self._api.list_cluster_custom_object,
                    group="scheduling.carbonshift.io",
                    version="v1alpha1",
                    plural="trafficschedules",
                    field_selector=field_selector,
                    timeout_seconds=90,
                )
                async for event in stream:
                    async with self._lock:
                        self._current = event["object"].get("status", {})
                    log.info("TrafficSchedule updated (watch)")
            except Exception as exc:  # noqa: BLE001
                log.error("Watch error: %s", exc)
                await asyncio.sleep(5)

    async def expiry_guard(self) -> None:
        while True:
            async with self._lock:
                valid_until = self._current.get("validUntil")
            try:
                expiry_dt = date_parser.isoparse(valid_until)
                sleep_s = max((expiry_dt - dt.datetime.utcnow()).total_seconds(), 0)
            except Exception:
                sleep_s = 60
            await asyncio.sleep(sleep_s + 1)
            await self.load_once()
    
    @property
    def consumption_enabled(self) -> bool:
        """
        True → consumare le queue.*,
        False → il consumer deve dormire.
        """
        return bool(self._current.get("consumption_enabled", 1))

    def seconds_to_expiry(self) -> float:
        """
        Ritorna quanti secondi mancano a validUntil (>=0),
        0 se il campo manca o non è parse-able.
        """
        valid_until = self._current.get("validUntil")
        try:
            expiry_dt = date_parser.isoparse(valid_until)
            return max((expiry_dt - dt.datetime.utcnow()).total_seconds(), 0)
        except Exception:
            return 0.0