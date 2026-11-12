"""聚合查询配置接口：维护查询模板，并提供一个受缓存裁决约束的执行入口。

缓存键由模板版本、参数区、数据集代次组成；同一模板的并发执行在缓存未完成时只允许
一次重算，其余请求等待同一份结果。执行超时、分页越界、代次变化都会返回明确失败。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, QueryExecutionPayload
from app.services.query import (
    QueryNotFound,
    QueryRejected,
    QueryService,
)
from app.services.query_cache import CacheGenerationChanged, QueryExecutionTimeout, query_cache
from app.store import store

router = APIRouter(prefix="/api/query", tags=["聚合查询配置"])

service = QueryService()

LIST_FIELDS = [
    "模板编号",
    "模板名称",
    "查询语句",
    "参数定义",
    "缓存时间",
    "结果上限",
    "执行超时",
    "模板版本",
    "数据集编号",
    "缓存代次",
    "数据集代次",
    "缓存状态",
    "最近缓存时间",
]
STATUSES = ["草稿", "待审批", "已发布", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待审批、已发布、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤聚合查询配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合查询配置清单：返回当前过滤条件下的全量数据（含缓存代次与失效状态）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "query", "total": total, "items": items}


@router.get("/cache/statistics")
def cache_statistics() -> dict[str, Any]:
    """缓存命中与单飞合并的观测数据，用于验证并发请求只触发一次计算。"""
    return {"统计": query_cache.stats(), "缓存条目": query_cache.entries()}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条查询模板明细；不存在时给出可读的错误说明。"""
    entry = service.find_by_ref(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/runtime", response_model=dict)
def get_runtime(entry_id: str) -> dict:
    """查看缓存时间、结果上限、执行超时的裁决结果与当前缓存代次。"""
    entry = service.find_by_ref(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    config = service.runtime_config(int(entry["id"]))
    if config is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return config


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条查询模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """对单条查询模板执行提交审批、发布模板、停用模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    target = service.find_by_ref(entry_id)
    if target is None:
        return ActionResult(ok=False, message=f"查询模板 {entry_id} 不存在或已归档")
    entry, message = service.run_action(int(target["id"]), action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/execute", response_model=dict)
def execute_entry(entry_id: str, payload: QueryExecutionPayload | None = None) -> dict:
    """执行查询模板：并发命中未完成时合并为一次重算，超时与分页越界返回明确失败。

    - 200：返回完整分页结果，带缓存键组成、缓存代次与失效状态。
    - 400：分页参数越界或超过结果上限，宁可不返回也不返回不完整分页。
    - 404：模板不存在。
    - 409：模板已停用，或计算期间数据集代次变化导致结果作废。
    - 504：执行超时，等待方同样得到超时失败，不读取旧缓存。
    """
    request = payload or QueryExecutionPayload()
    entry = service.find_by_ref(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    try:
        return service.execute(
            int(entry["id"]),
            params=request.params,
            page=request.page,
            size=request.size,
            delay_ms=request.delay_ms,
        )
    except QueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QueryRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CacheGenerationChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QueryExecutionTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc


@router.post("/{entry_id}/cache/invalidate", response_model=dict)
def invalidate_cache(entry_id: str, payload: EntryPayload | None = None) -> dict:
    """让该模板的旧缓存键立即失效，并持久推进缓存代次；旧结果不会再回显。"""
    entry = service.find_by_ref(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    reason = str((payload.values if payload else {}).get("reason") or "手动失效")
    removed, generation = service.invalidate_cache(str(entry.get("模板编号")), reason=reason)
    return {
        "ok": True,
        "message": "缓存已失效，旧键不会再命中",
        "模板编号": entry.get("模板编号"),
        "失效键数": removed,
        "缓存代次": generation,
        "失效状态": "已失效",
    }


@router.get("/{entry_id}/cache", response_model=dict)
def describe_cache(entry_id: str) -> dict:
    """查看该模板当前的缓存条目、缓存代次与失效状态。"""
    entry = service.find_by_ref(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    template_no = str(entry.get("模板编号"))
    return {
        "模板编号": template_no,
        "缓存代次": store.query_cache_generation(template_no),
        "失效状态": store.query_cache_state(template_no),
        "缓存条目": query_cache.entries_for_template(template_no),
        "执行统计": query_cache.stats(template_no),
    }
