"""只追加（append-only）事件账本。

所有事实变更都以事件形式追加，事件一经接受即不可变；更正通过新的
更正/裁决事件实现，并必须引用原始证据。重放事件即可重建任意时刻的状态。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


def now_utc() -> str:
    """当前 UTC 时间（ISO-8601，秒精度）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class Event:
    """不可变事件记录。

    Attributes:
        id: 事件唯一编号，也是更正/裁决必须引用的证据编号。
        type: 事件类型（见 EventStore.append 的用法约定）。
        payload: 事件数据，仅包含原始事实。
        occurred_at: 事实发生时间（由调用方给出，用于排序与复现）。
        recorded_at: 事件被系统接受的时间（账本时间戳，不可改）。
        actor: 记录来源（设备编号 / 裁判编号 / 系统）。
        causation_id: 产生该事件的上游事件（如更正指向原始事件）。
    """

    id: str
    type: str
    payload: dict[str, Any]
    occurred_at: str
    recorded_at: str
    actor: str
    causation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "causation_id": self.causation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            id=data["id"],
            type=data["type"],
            payload=dict(data["payload"]),
            occurred_at=data["occurred_at"],
            recorded_at=data["recorded_at"],
            actor=data["actor"],
            causation_id=data.get("causation_id"),
        )


class EventStore:
    """线程安全的内存只追加账本，可整体序列化。"""

    def __init__(self, clock: Callable[[], str] = now_utc):
        self._events: list[Event] = []
        self._lock = threading.RLock()
        self._clock = clock

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        occurred_at: str | None = None,
        actor: str = "system",
        causation_id: str | None = None,
        event_id: str | None = None,
    ) -> Event:
        """追加一条事件。occurred_at 缺省取当前时间。"""
        event = Event(
            id=event_id or new_event_id(),
            type=event_type,
            payload=dict(payload),
            occurred_at=occurred_at or self._clock(),
            recorded_at=self._clock(),
            actor=actor,
            causation_id=causation_id,
        )
        with self._lock:
            self._events.append(event)
        return event

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            for event in self._events:
                if event.id == event_id:
                    return event
        return None

    def replay(self, handler: Callable[[Event], None], *, until: str | None = None) -> None:
        """按记录顺序重放事件；until 给出时只重放 occurred_at <= until 的事件。"""
        with self._lock:
            events = list(self._events)
        for event in events:
            if until is not None and event.occurred_at > until:
                break
            handler(event)

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.all()]

    @classmethod
    def from_list(cls, items: Iterable[dict[str, Any]]) -> "EventStore":
        store = cls()
        for item in items:
            event = Event.from_dict(item)
            with store._lock:
                store._events.append(event)
        return store
