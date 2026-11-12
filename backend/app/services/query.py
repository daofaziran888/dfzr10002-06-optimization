"""聚合查询配置业务规则：状态流转、字段校验、查询执行与缓存裁决。

执行链路：查询模板 -> 数据集代次 -> 查询缓存 -> 数据集刷新失效 -> 缓存规则清单同步。
缓存键只由模板版本、参数区、数据集代次组成，分页与执行选项不参与缓存键。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from app.store import store
from app.services.query_cache import (
    CacheGenerationChanged,
    QueryExecutionTimeout,
    build_cache_key,
    canonical_params,
    query_cache,
)

MODULE = "query"
REQUIRED_FIELDS = ["模板编号", "模板名称", "查询语句"]
STATUS_ORDER = ["草稿", "待审批", "已发布", "已停用"]
ACTION_RULES = {"提交审批": "待审批", "发布模板": "已发布", "停用模板": "已停用"}
NEGATIVE_ACTIONS = ["停用模板"]

DEFAULT_RESULT_LIMIT = 100
RESULT_LIMIT_HARD_CAP = 500
DEFAULT_EXECUTION_TIMEOUT = 5.0
EXECUTION_TIMEOUT_MAX = 30.0
DEFAULT_TTL = 60.0

# 三类配置发生冲突时的统一裁决顺序，接口会原样返回给调用方。
RUNTIME_RULE_ORDER = [
    "执行超时优先：任何一次重算都必须在该时限内完成，超时即失败，且不读取、不写入缓存。",
    "结果上限优先于分页：先按上限截断结果，再按页返回；分页范围越界时直接失败，不返回不完整分页。",
    "缓存时间只作用于完整结果：缓存时间为 0 表示不缓存；缓存代次或数据集代次变化后旧键立即失效。",
]


@dataclass
class RuntimeConfig:
    ttl: float
    result_limit: int
    timeout: float
    cache_enabled: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "缓存时间": self.ttl,
            "结果上限": self.result_limit,
            "执行超时": self.timeout,
            "是否缓存": self.cache_enabled,
            "裁决顺序": list(RUNTIME_RULE_ORDER),
            "裁决说明": list(self.notes),
        }


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def resolve_runtime_config(entry: dict[str, Any]) -> RuntimeConfig:
    """把模板上的缓存时间、结果上限、执行超时裁决成一套可执行配置。

    裁决顺序固定为：执行超时 > 结果上限 > 缓存时间。任何一项缺省或非法都给出
    明确的回退值，而不是让查询在运行时悄悄失效。
    """
    notes: list[str] = []

    raw_limit = _as_number(entry.get("结果上限"))
    if raw_limit is None or raw_limit <= 0:
        limit = DEFAULT_RESULT_LIMIT
        notes.append(f"结果上限缺失或非法，已裁决为默认 {limit} 条")
    else:
        limit = int(raw_limit)
        if limit > RESULT_LIMIT_HARD_CAP:
            notes.append(f"结果上限 {limit} 超过硬上限，已裁决截到 {RESULT_LIMIT_HARD_CAP} 条")
            limit = RESULT_LIMIT_HARD_CAP

    raw_timeout = _as_number(entry.get("执行超时"))
    if raw_timeout is None or raw_timeout <= 0:
        timeout = DEFAULT_EXECUTION_TIMEOUT
        notes.append(f"执行超时缺失或非法，已裁决为默认 {timeout:g} 秒")
    else:
        timeout = raw_timeout
        if timeout > EXECUTION_TIMEOUT_MAX:
            notes.append(f"执行超时 {timeout:g} 秒超过上限，已裁决截到 {EXECUTION_TIMEOUT_MAX:g} 秒")
            timeout = EXECUTION_TIMEOUT_MAX

    raw_ttl = _as_number(entry.get("缓存时间"))
    if raw_ttl is None:
        ttl = DEFAULT_TTL
        notes.append(f"缓存时间缺失或非法，已裁决为默认 {ttl:g} 秒")
    elif raw_ttl <= 0:
        ttl = 0.0
        notes.append("缓存时间不大于 0，已裁决为不缓存：每次请求都重新计算")
    else:
        ttl = raw_ttl

    cache_enabled = ttl > 0
    if cache_enabled and raw_ttl is not None and raw_ttl < 1:
        notes.append("缓存时间不足 1 秒，按 1 秒内有效处理；过期后不再回显旧结果")
    if str(entry.get("status") or "") != "已发布" and cache_enabled:
        notes.append("模板尚未发布，已裁决为只执行不写缓存")
        cache_enabled = False
    if str(entry.get("status") or "") == "已停用":
        notes.append("模板已停用，执行请求会被拒绝")
    if not notes:
        notes.append("缓存时间、结果上限与执行超时无冲突，按裁决顺序直接启用")
    return RuntimeConfig(
        ttl=ttl,
        result_limit=limit,
        timeout=timeout,
        cache_enabled=cache_enabled,
        notes=notes,
    )


# 模拟底层分析数据：同一个数据集代次下，参数区决定返回行，代次变化会改变数值。
_REGIONS = ["华东", "华南", "华北", "西部", "东北"]
_ALL_REGIONS = ["华东", "华南", "华北", "西部", "东北", "西南", "西北", "华中", "港澳台", "海外"]
_MONTHS = [f"2026-{month:02d}" for month in range(1, 13)]


def _amount(region: str, month: str, generation: int) -> int:
    seed = sum(ord(ch) for ch in region) * 31 + int(month[-2:]) * 17
    return (seed * generation) % 9000 + 1000


def _dataset_rows(dataset_no: str, params: dict[str, Any], generation: int) -> list[dict[str, Any]]:
    """按数据集编号、参数区和代次生成稳定的聚合结果。

    返回完整结果（不在这里截断），截断与分页统一由执行层处理，保证分页边界一致。
    """
    region = str(params.get("region") or params.get("地区") or "全部")
    all_mark = region in {"全部", "全国", "*", "all", "ALL"}
    regions = _ALL_REGIONS if all_mark else [region]
    rows: list[dict[str, Any]] = []
    for region_name in regions:
        for month in _MONTHS:
            rows.append(
                {
                    "数据集": dataset_no,
                    "地区": region_name,
                    "月份": month,
                    "金额": _amount(region_name, month, generation),
                }
            )
    return rows


class QueryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._enrich(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return None if entry is None else self._enrich(entry)

    def find_by_ref(self, ref: str | int) -> dict[str, Any] | None:
        """支持用内部 id 或模板编号（如 QUERY-0004、QUER-0001）定位模板。"""
        text = str(ref)
        if text.isdigit():
            row = store.find(MODULE, int(text))
        else:
            row = next(
                (item for item in store.rows(MODULE) if str(item.get("模板编号")) == text), None
            )
        return None if row is None else self._enrich(row)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("参数定义", "缓存时间", "结果上限", "执行超时", "模板版本", "数据集编号"):
            if field in values and values.get(field) is not None:
                entry[field] = values.get(field)
        entry.setdefault("模板版本", "v1")
        entry.setdefault("数据集编号", "DATA-0001")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        self.sync_cache_rules(entry)
        return self._enrich(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合查询配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "停用模板":
            self.invalidate_cache(str(entry.get("模板编号")), reason="模板已停用")
        # 状态变化会影响是否允许缓存，缓存规则清单随之同步。
        self.sync_cache_rules(entry)
        return self._enrich(entry), f"查询模板已{action}"

    # ---- 缓存代次与清单同步 ---------------------------------------------

    def _enrich(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = dict(entry)
        template_no = str(item.get("模板编号", ""))
        dataset_no = str(item.get("数据集编号") or "")
        item["缓存代次"] = store.query_cache_generation(template_no)
        item["数据集代次"] = store.dataset_generation(dataset_no) if dataset_no else 0
        item["缓存状态"] = store.query_cache_state(template_no)
        return item

    def runtime_config(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        config = resolve_runtime_config(entry)
        return {
            "模板编号": entry.get("模板编号"),
            "模板版本": entry.get("模板版本") or "v1",
            "数据集编号": entry.get("数据集编号"),
            "缓存代次": store.query_cache_generation(str(entry.get("模板编号", ""))),
            "数据集代次": store.dataset_generation(str(entry.get("数据集编号") or "")),
            **config.as_dict(),
        }

    def sync_cache_rules(self, entry: dict[str, Any]) -> None:
        """把查询模板的缓存代次、数据集代次与失效状态同步进缓存规则清单。"""
        from app.services.cache import CacheService  # 避免模块级循环导入

        CacheService().sync_for_template(entry)

    def invalidate_cache(self, template_no: str, reason: str = "手动失效") -> tuple[int, int]:
        """让该模板的旧缓存键立即失效，并持久推进缓存代次。

        返回 (失效的旧键数量, 新的缓存代次)。
        """
        removed = query_cache.invalidate_template(template_no)
        generation = store.bump_query_cache_generation(template_no)
        store.set_query_cache_state(template_no, "已失效", reason)
        entry = next(
            (row for row in store.rows(MODULE) if str(row.get("模板编号")) == template_no), None
        )
        if entry is not None:
            self.sync_cache_rules(entry)
        return removed, generation

    def invalidate_for_dataset(self, dataset_no: str, generation: int, reason: str) -> list[str]:
        """数据集刷新后，所有绑定它的查询模板同步失效。"""
        affected = [
            str(row.get("模板编号"))
            for row in store.rows(MODULE)
            if str(row.get("数据集编号") or "") == dataset_no
        ]
        for template_no in affected:
            query_cache.invalidate_template(template_no)
            store.bump_query_cache_generation(template_no)
            store.set_query_cache_state(template_no, "已失效", reason)
            entry = next(
                (row for row in store.rows(MODULE) if str(row.get("模板编号")) == template_no), None
            )
            if entry is not None:
                self.sync_cache_rules(entry)
        return affected

    def mark_cache_valid(self, template_no: str) -> None:
        store.set_query_cache_state(template_no, "有效", "—")

    # ---- 执行 -----------------------------------------------------------

    def execute(
        self,
        entry_id: int,
        *,
        params: dict[str, Any] | None = None,
        page: int = 1,
        size: int = 20,
        delay_ms: int = 0,
    ) -> dict[str, Any]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            raise QueryNotFound(f"查询模板 {entry_id} 不存在或已归档")
        if str(entry.get("status") or "") == "已停用":
            raise QueryRejected("模板已停用，执行请求被拒绝")

        config = resolve_runtime_config(entry)
        params = dict(params or {})
        template_no = str(entry.get("模板编号"))
        template_version = str(entry.get("模板版本") or "v1")
        dataset_no = str(entry.get("数据集编号") or "DATA-0001")
        dataset_generation = store.dataset_generation(dataset_no)
        cache_generation = store.query_cache_generation(template_no)

        self._validate_page(config.result_limit, page, size)

        key_parts = {
            "模板编号": template_no,
            "模板版本": template_version,
            "参数区": canonical_params(params),
            "数据集代次": dataset_generation,
        }
        key = build_cache_key(key_parts)

        delay_ms = max(int(delay_ms or 0), 0)

        def compute(deadline: float) -> list[dict[str, Any]]:
            """模拟底层聚合计算；结果先按结果上限截断，再进入缓存。"""
            if delay_ms:
                self._sleep_with_deadline(delay_ms / 1000.0, deadline)
            rows = _dataset_rows(dataset_no, params, dataset_generation)
            return rows[: config.result_limit]

        def validate_generation() -> None:
            current_version = str(entry.get("模板版本") or "v1")
            current_generation = store.dataset_generation(dataset_no)
            current_cache_generation = store.query_cache_generation(template_no)
            if (
                current_version != template_version
                or current_generation != dataset_generation
                or current_cache_generation != cache_generation
            ):
                raise CacheGenerationChanged(
                    "计算期间模板版本、数据集代次或缓存代次已变化，本次结果丢弃，不写入缓存，请重试"
                )

        started_at = time.monotonic()
        if not config.cache_enabled:
            # 不缓存路径同样受执行超时约束，且不会用任何旧缓存冒充新结果。
            deadline = started_at + config.timeout
            rows = compute(deadline)
            validate_generation()
            elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
            return self._build_response(
                entry=entry,
                config=config,
                key=key,
                key_parts=key_parts,
                page=page,
                size=size,
                rows=rows,
                cache_generation=store.query_cache_generation(template_no),
                cache_status="未缓存：模板裁决为不缓存",
                invalid_state="未启用",
                elapsed_ms=elapsed_ms,
            )

        started = time.monotonic()
        try:
            entry_cache, status, computed = query_cache.execute(
                template_no=template_no,
                key_parts=key_parts,
                dataset_no=dataset_no,
                dataset_generation=dataset_generation,
                cache_generation=cache_generation,
                ttl=config.ttl,
                timeout=config.timeout,
                compute=compute,
                validate_generation=validate_generation,
            )
        except QueryExecutionTimeout:
            # 超时只影响本次请求：没有新鲜的旧结果才标记失效，绝不回放旧缓存。
            if not query_cache.has_fresh_entries(template_no):
                store.set_query_cache_state(template_no, "已失效", "执行超时，未写入缓存")
            self.sync_cache_rules(entry)
            raise
        except CacheGenerationChanged:
            store.set_query_cache_state(template_no, "已失效", "数据集或模板代次变化")
            self.sync_cache_rules(entry)
            raise

        store.set_query_cache_state(template_no, "有效", "—")
        entry["最近缓存时间"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self.sync_cache_rules(entry)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return self._build_response(
            entry=entry,
            config=config,
            key=key,
            key_parts=key_parts,
            page=page,
            size=size,
            rows=entry_cache.rows,
            cache_generation=entry_cache.cache_generation,
            cache_status=status,
            invalid_state="有效",
            elapsed_ms=elapsed_ms,
            computed=computed,
            cache_key=entry_cache.key,
            hit_statistics=query_cache.stats(template_no),
        )

    @staticmethod
    def _sleep_with_deadline(seconds: float, deadline: float) -> None:
        remaining = seconds
        while remaining > 0:
            if time.monotonic() >= deadline:
                raise QueryExecutionTimeout("执行超时：查询在时限内未完成，本次请求失败")
            time.sleep(min(remaining, 0.05))
            remaining -= 0.05

    @staticmethod
    def _validate_page(result_limit: int, page: int, size: int) -> None:
        if page < 1:
            raise QueryRejected(f"页码 page={page} 非法，必须从 1 开始")
        if size < 1:
            raise QueryRejected(f"每页条数 size={size} 非法，必须大于 0")
        if size > result_limit:
            raise QueryRejected(
                f"每页条数 size={size} 超过结果上限 {result_limit}，无法返回完整分页，请缩小分页范围"
            )
        offset = (page - 1) * size
        if offset >= result_limit:
            raise QueryRejected(
                f"分页越界：第 {page} 页起始位置 {offset} 已达到结果上限 {result_limit}，请缩小页码或每页条数"
            )
        if offset + size > result_limit:
            raise QueryRejected(
                f"分页越界：第 {page} 页请求 [{offset}, {offset + size}) 超出结果上限 {result_limit}，"
                "不返回不完整分页，请缩小每页条数"
            )

    def _build_response(
        self,
        *,
        entry: dict[str, Any],
        config: RuntimeConfig,
        key: str,
        key_parts: dict[str, Any],
        page: int,
        size: int,
        rows: list[dict[str, Any]],
        cache_generation: int,
        cache_status: str,
        invalid_state: str,
        elapsed_ms: float,
        computed: bool = False,
        cache_key: str | None = None,
        hit_statistics: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        total = len(rows)
        offset = (page - 1) * size
        page_rows = rows[offset:offset + size]
        dataset_no = str(entry.get("数据集编号") or "")
        return {
            "ok": True,
            "模板编号": entry.get("模板编号"),
            "模板名称": entry.get("模板名称"),
            "模板版本": str(entry.get("模板版本") or "v1"),
            "参数区": key_parts["参数区"],
            "数据集编号": dataset_no,
            "数据集代次": store.dataset_generation(dataset_no),
            "缓存键": cache_key or key,
            "缓存键组成": dict(key_parts),
            "缓存代次": cache_generation,
            "失效状态": invalid_state,
            "缓存状态": cache_status,
            "本次是否重算": computed,
            "是否命中缓存": cache_status == "命中缓存",
            "缓存时间": config.ttl,
            "结果上限": config.result_limit,
            "执行超时": config.timeout,
            "已截断": total >= config.result_limit,
            "裁决": config.as_dict(),
            "page": page,
            "size": size,
            "offset": offset,
            "total": total,
            "返回条数": len(page_rows),
            "items": page_rows,
            "rows": page_rows,
            "耗时毫秒": elapsed_ms,
            "执行统计": hit_statistics or {},
        }


class QueryNotFound(LookupError):
    """模板不存在。"""


class QueryRejected(RuntimeError):
    """分页或配置不满足执行条件。"""


query_service = QueryService()

# 供仍持有 module 级变量的旧调用点复用。
service = query_service
