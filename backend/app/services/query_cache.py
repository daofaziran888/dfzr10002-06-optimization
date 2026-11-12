"""查询模板缓存运行时：单飞合并、执行超时与代次失效。

设计要点：
- 缓存键 = 模板编号 + 模板版本 + 参数区（规范化 JSON）+ 数据集代次，SQL 相同但参数区
  不同不会互相命中。
- 同一个键上的并发请求只允许一个线程真正计算（single-flight），其余请求等待同一份
  结果；等待方不读取任何旧缓存。
- 每个请求都有自己的执行超时截止时间。计算未在超时内完成时，发起方和等待方都拿到
  明确的超时失败，不会回退到过期缓存。
- 结果在写入前先确认模板版本与数据集代次没有变化，否则丢弃本次计算并报代次冲突。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from app.store import store


class QueryExecutionTimeout(RuntimeError):
    """执行超时：计算或等待超过模板的执行超时。"""


class CacheGenerationChanged(RuntimeError):
    """计算期间模板版本或数据集代次发生变化，本次结果不得写入缓存。"""


def canonical_params(params: Any) -> str:
    """把参数区规范化成稳定字符串；键顺序、空白不同但语义相同的参数视为同一组。"""
    return json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def cache_key_parts(
    *,
    template_no: str,
    template_version: str,
    params: dict[str, Any],
    dataset_generation: int,
) -> dict[str, Any]:
    """缓存键的完整组成，接口会把这几项原样回显，便于确认互不串键。"""
    return {
        "模板编号": template_no,
        "模板版本": template_version,
        "参数区": canonical_params(params),
        "数据集代次": int(dataset_generation),
    }


def build_cache_key(parts: dict[str, Any]) -> str:
    raw = canonical_params(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    key: str
    parts: dict[str, Any]
    rows: list[dict[str, Any]]
    template_no: str
    template_version: str
    params_json: str
    dataset_no: str
    dataset_generation: int
    cache_generation: int
    ttl: float
    created_at: float
    state: str = "有效"
    hit_count: int = 0
    last_hit_at: float | None = None

    def fresh(self, now: float) -> bool:
        return self.state == "有效" and self.ttl > 0 and now < self.created_at + self.ttl

    def describe(self, now: float) -> dict[str, Any]:
        return {
            "缓存键": self.key,
            "缓存键组成": dict(self.parts),
            "模板编号": self.template_no,
            "模板版本": self.template_version,
            "数据集代次": self.dataset_generation,
            "缓存代次": self.cache_generation,
            "缓存时间": self.ttl,
            "已存活秒": round(now - self.created_at, 3),
            "剩余秒": max(round(self.created_at + self.ttl - now, 3), 0) if self.ttl > 0 else 0,
            "失效状态": self.state,
            "命中次数": self.hit_count,
            "结果条数": len(self.rows),
        }


@dataclass
class _Flight:
    key: str
    event: threading.Event = field(default_factory=threading.Event)
    result: CacheEntry | None = None
    error: BaseException | None = None
    abandoned: bool = False
    started_at: float = field(default_factory=time.monotonic)


class QueryCacheRuntime:
    """进程内查询缓存 + 单飞调度器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, CacheEntry] = {}
        self._flights: dict[str, _Flight] = {}
        # 计算线程池只负责真正的聚合计算；每个请求的等待在自己的线程里完成，
        # 因此十个并发请求也只占用一个计算线程。
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="query-cache")
        # 观测指标：用于验证“十个并发请求只触发一次计算”。
        self._stats: dict[str, dict[str, int]] = {}
        self._global_stats = {
            "计算次数": 0,
            "缓存命中次数": 0,
            "合并等待次数": 0,
            "超时失败次数": 0,
            "代次失效次数": 0,
        }

    # ---- 统计与观测 -----------------------------------------------------

    def _template_stats(self, template_no: str) -> dict[str, int]:
        return self._stats.setdefault(
            template_no,
            {
                "计算次数": 0,
                "缓存命中次数": 0,
                "合并等待次数": 0,
                "超时失败次数": 0,
                "代次失效次数": 0,
            },
        )

    def _bump(self, template_no: str, metric: str) -> None:
        with self._lock:
            self._template_stats(template_no)[metric] += 1
            self._global_stats[metric] += 1

    def stats(self, template_no: str | None = None) -> dict[str, int]:
        with self._lock:
            if template_no is None:
                return dict(self._global_stats)
            return dict(self._template_stats(template_no))

    def entries_for_template(self, template_no: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            entries = [entry for entry in self._entries.values() if entry.template_no == template_no]
            return [entry.describe(now) for entry in entries]

    def fresh_hit_count(self) -> int:
        """当前仍在有效期内的缓存条目数。"""
        now = time.monotonic()
        with self._lock:
            return sum(1 for entry in self._entries.values() if entry.fresh(now))

    def has_fresh_entries(self, template_no: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(
                entry.template_no == template_no and entry.fresh(now)
                for entry in self._entries.values()
            )

    def entries(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [entry.describe(now) for entry in self._entries.values()]

    # ---- 失效 -----------------------------------------------------------

    def invalidate_template(self, template_no: str) -> int:
        with self._lock:
            keys = [key for key, entry in self._entries.items() if entry.template_no == template_no]
            for key in keys:
                entry = self._entries.pop(key, None)
                if entry is not None:
                    entry.state = "已失效"
            if keys:
                self._global_stats["代次失效次数"] += 1
                self._template_stats(template_no)["代次失效次数"] += 1
            return len(keys)

    def invalidate_dataset(self, dataset_no: str) -> int:
        with self._lock:
            keys = [key for key, entry in self._entries.items() if entry.dataset_no == dataset_no]
            for key in keys:
                entry = self._entries.pop(key, None)
                if entry is not None:
                    entry.state = "已失效"
                    self._template_stats(entry.template_no)["代次失效次数"] += 1
            if keys:
                self._global_stats["代次失效次数"] += 1
            return len(keys)

    # ---- 命中与单飞 -----------------------------------------------------

    def _take_fresh_hit(self, key: str, now: float, template_no: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if not entry.fresh(now):
            # 过期条目立即剔除，绝不再回显；失效状态与缓存代次同步记录。
            self._entries.pop(key, None)
            entry.state = "已失效"
            if not any(
                other.template_no == entry.template_no and other.fresh(now)
                for other in self._entries.values()
            ):
                store.set_query_cache_state(entry.template_no, "已失效", "缓存已过期")
            return None
        entry.hit_count += 1
        entry.last_hit_at = now
        self._template_stats(template_no)["缓存命中次数"] += 1
        self._global_stats["缓存命中次数"] += 1
        return entry

    def execute(
        self,
        *,
        template_no: str,
        key_parts: dict[str, Any],
        dataset_no: str,
        dataset_generation: int,
        cache_generation: int,
        ttl: float,
        timeout: float,
        compute: Callable[[float], list[dict[str, Any]]],
        validate_generation: Callable[[], None],
    ) -> tuple[CacheEntry, str, bool]:
        """返回 (缓存条目, 状态, 本次请求是否真正触发计算)。

        状态取值：命中缓存 / 等待合并 / 未命中重算。
        """
        if timeout <= 0:
            raise QueryExecutionTimeout("执行超时配置必须大于 0 秒，无法开始查询")
        key = build_cache_key(key_parts)
        now = time.monotonic()

        with self._lock:
            hit = self._take_fresh_hit(key, now, template_no)
            if hit is not None:
                return hit, "命中缓存", False
            flight = self._flights.get(key)
            owner = flight is None
            if owner:
                flight = _Flight(key=key)
                self._flights[key] = flight

        if not owner:
            # 等待同一个键上的那份结果；等待上限同样是执行超时。
            return self._await_flight(flight, key, template_no, timeout)

        deadline = time.monotonic() + timeout

        def run() -> None:
            try:
                rows = compute(deadline)
                if time.monotonic() > deadline:
                    # 计算在超时之后才结束：结果作废，不写入缓存，也不交给等待方。
                    raise QueryExecutionTimeout(
                        f"执行超时：查询在 {timeout:g} 秒内未完成，本次结果作废"
                    )
                with self._lock:
                    if flight.abandoned:
                        return
                    # 结果写入前再校验一次：模板版本 / 数据集代次必须仍是同一个。
                    validate_generation()
                    entry = CacheEntry(
                        key=key,
                        parts=dict(key_parts),
                        rows=rows,
                        template_no=template_no,
                        template_version=str(key_parts["模板版本"]),
                        params_json=str(key_parts["参数区"]),
                        dataset_no=dataset_no,
                        dataset_generation=dataset_generation,
                        cache_generation=cache_generation,
                        ttl=ttl,
                        created_at=time.monotonic(),
                    )
                    self._entries[key] = entry
                    flight.result = entry
            except BaseException as exc:  # noqa: BLE001 - 原样转交给所有等待方
                with self._lock:
                    if not flight.abandoned:
                        flight.error = exc
            finally:
                with self._lock:
                    if self._flights.get(key) is flight:
                        self._flights.pop(key, None)
                flight.event.set()

        self._bump(template_no, "计算次数")
        self._executor.submit(run)

        if not flight.event.wait(timeout):
            self._bump(template_no, "超时失败次数")
            with self._lock:
                flight.abandoned = True
                flight.error = QueryExecutionTimeout(
                    f"执行超时：查询在 {timeout:g} 秒内未完成，本次请求失败且不读取旧缓存"
                )
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
            flight.event.set()
            raise flight.error

        if flight.error is not None:
            if isinstance(flight.error, QueryExecutionTimeout):
                self._bump(template_no, "超时失败次数")
            raise flight.error
        if flight.result is None:
            raise QueryExecutionTimeout("查询执行未返回结果，本次请求失败")
        return flight.result, "未命中重算", True

    def _await_flight(
        self, flight: _Flight, key: str, template_no: str, timeout: float
    ) -> tuple[CacheEntry, str, bool]:
        notified = flight.event.wait(timeout)
        if not notified:
            self._bump(template_no, "超时失败次数")
            raise QueryExecutionTimeout(
                f"执行超时：等待同一查询结果超过 {timeout:g} 秒，本次请求失败且不读取旧缓存"
            )
        if flight.error is not None:
            if isinstance(flight.error, QueryExecutionTimeout):
                self._bump(template_no, "超时失败次数")
            raise flight.error
        if flight.result is None:
            raise QueryExecutionTimeout("查询执行未返回结果，本次请求失败")
        self._bump(template_no, "合并等待次数")
        return flight.result, "等待合并", False


query_cache = QueryCacheRuntime()
