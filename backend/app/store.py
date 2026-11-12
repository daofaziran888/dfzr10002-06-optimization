"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 代次状态独立于列表行本身：列表反复读取、页面刷新都不会把它重置。
        self._lock = threading.RLock()
        self._dataset_generations: dict[str, int] = {}
        self._query_cache_generations: dict[str, int] = {}
        self._query_cache_states: dict[str, str] = {}
        self._query_cache_state_details: dict[str, dict[str, Any]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    # ---- 代次 -------------------------------------------------------------

    def dataset_generation(self, dataset_no: str) -> int:
        """数据集代次：每次刷新递增，缓存键使用它，旧键随之失效。"""
        with self._lock:
            return self._dataset_generations.setdefault(dataset_no, 1)

    def bump_dataset_generation(self, dataset_no: str) -> int:
        with self._lock:
            generation = self._dataset_generations.get(dataset_no, 1) + 1
            self._dataset_generations[dataset_no] = generation
            return generation

    def query_cache_generation(self, template_no: str) -> int:
        """查询模板缓存代次：每次失效递增，并持久保存在仓库状态里。"""
        with self._lock:
            return self._query_cache_generations.setdefault(template_no, 1)

    def bump_query_cache_generation(self, template_no: str) -> int:
        with self._lock:
            generation = self._query_cache_generations.get(template_no, 1) + 1
            self._query_cache_generations[template_no] = generation
            return generation

    def query_cache_state(self, template_no: str) -> str:
        """缓存代次对应的失效状态；没有记录过失效就是有效。"""
        with self._lock:
            return self._query_cache_states.get(template_no, "有效")

    def set_query_cache_state(self, template_no: str, state: str, reason: str) -> None:
        with self._lock:
            self._query_cache_states[template_no] = state
            entry = self._query_cache_state_details.get(template_no)
            if entry is None:
                entry = {}
                self._query_cache_state_details[template_no] = entry
            entry["失效状态"] = state
            entry["最近失效"] = reason
            entry["失效时间"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def query_cache_state_details(self, template_no: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._query_cache_state_details.get(template_no, {}))

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
