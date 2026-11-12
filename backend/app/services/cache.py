"""查询缓存管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["规则编号", "缓存目标", "缓存时间"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class CacheService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._enrich(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return None if entry is None else self._enrich(entry)

    def find_by_template(self, template_no: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in store.rows(MODULE)
                if str(row.get("模板编号") or "") == template_no
                or str(row.get("缓存目标") or "").startswith(template_no)
            ),
            None,
        )

    def _enrich(self, row: dict[str, Any]) -> dict[str, Any]:
        """缓存规则上的代次与失效状态跟查询模板保持一致，不各说各话。"""
        item = dict(row)
        template_no = str(item.get("模板编号") or "")
        dataset_no = str(item.get("数据集编号") or "")
        details = store.query_cache_state_details(template_no) if template_no else {}
        if template_no:
            item["缓存代次"] = store.query_cache_generation(template_no)
            item["失效状态"] = store.query_cache_state(template_no)
            if "最近失效" in details:
                item["最近失效"] = details.get("最近失效")
                item["失效时间"] = details.get("失效时间")
            elif "最近失效" in item:
                item["最近失效"] = item.get("最近失效")
        if dataset_no:
            item["数据集代次"] = store.dataset_generation(dataset_no)
        return item

    def sync_for_template(self, template: dict[str, Any]) -> dict[str, Any] | None:
        """把查询模板的缓存代次、数据集代次与失效状态写回对应的缓存规则。"""
        template_no = str(template.get("模板编号") or "")
        if not template_no:
            return None
        dataset_no = str(template.get("数据集编号") or "")
        rule = self.find_by_template(template_no)
        state = store.query_cache_state(template_no)
        details = store.query_cache_state_details(template_no)
        if rule is None:
            next_id = max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1
            rule = {
                "id": next_id,
                "status": "已启用" if template.get("status") == "已发布" else "待校验",
                "pending": template.get("status") != "已发布",
                "abnormal": False,
                "规则编号": f"CACH-{next_id:04d}",
                "缓存目标": f"{template_no} {template.get('模板名称') or ''}".strip(),
                "缓存时间": template.get("缓存时间"),
                "失效条件": f"数据集 {dataset_no or '未绑定'} 代次变化即失效",
                "预加载设置": "不预加载",
                "内存上限": template.get("结果上限"),
                "淘汰策略": "按代次淘汰",
                "是否启用": "是" if template.get("status") == "已发布" else "否",
                "模板编号": template_no,
                "数据集编号": dataset_no,
            }
            store.rows(MODULE).append(rule)
        rule["缓存时间"] = template.get("缓存时间")
        rule["内存上限"] = template.get("结果上限")
        rule["模板编号"] = template_no
        rule["数据集编号"] = dataset_no
        rule["数据集代次"] = store.dataset_generation(dataset_no) if dataset_no else 0
        rule["缓存代次"] = store.query_cache_generation(template_no)
        rule["失效状态"] = state
        rule["是否启用"] = "是" if template.get("status") == "已发布" else "否"
        if details:
            rule["最近失效"] = details.get("最近失效")
            rule["失效时间"] = details.get("失效时间")
        if state == "有效":
            rule["最近失效"] = "—"
        rule["pending"] = template.get("status") != "已发布"
        return rule

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询缓存管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "停用配置" and entry.get("模板编号"):
            from app.services.query import QueryService  # 避免模块级循环导入

            QueryService().invalidate_cache(str(entry.get("模板编号")), reason="缓存规则已停用")
        return self._enrich(entry), f"缓存规则已{action}"
