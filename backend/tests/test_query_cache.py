"""查询模板缓存运行时回归测试：单飞、超时、代次失效、分页边界与清单同步。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import time
import unittest

from app.services.cache import CacheService
from app.services.dataset import DatasetService
from app.services.query import QueryRejected, QueryService
from app.services.query_cache import QueryExecutionTimeout, query_cache
from app.store import store


def _run_concurrently(count: int, fn) -> list:
    results: list = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - 测试需要区分失败类型
            value = exc
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


class QueryCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.query = QueryService()
        self.cache = CacheService()
        self.dataset = DatasetService()
        self.template = self.query.find_by_ref("QUERY-0004")
        self.template_no = "QUERY-0004"
        self.dataset_no = "DATA-0003"

    def _execute(self, **kwargs):
        return self.query.execute(int(self.template["id"]), **kwargs)

    def test_ten_concurrent_requests_compute_once(self) -> None:
        params = {"region": "华北"}
        before = query_cache.stats(self.template_no)["计算次数"]

        def call():
            return self._execute(params=params, page=1, size=10, delay_ms=300)

        results = _run_concurrently(10, call)
        self.assertEqual(len(results), 10)
        for result in results:
            self.assertNotIsInstance(result, Exception)
        keys = {result["缓存键"] for result in results}
        self.assertEqual(len(keys), 1, "同一模板同一参数区必须落在同一个缓存键")
        self.assertEqual(sum(1 for result in results if result["本次是否重算"]), 1)
        stats = self.query.find_by_ref(self.template_no)
        self.assertEqual(results[0]["执行统计"]["计算次数"] - before, 1)
        self.assertEqual(stats["缓存状态"], "有效")
        first_rows = results[0]["items"]
        for result in results:
            self.assertEqual(result["items"], first_rows)

    def test_result_limit_never_returns_101(self) -> None:
        result = self._execute(params={"region": "全部"}, page=1, size=100)
        self.assertLessEqual(result["返回条数"], 100)
        self.assertEqual(result["返回条数"], 100)
        self.assertEqual(result["total"], 100)
        self.assertTrue(result["已截断"])

    def test_pagination_beyond_limit_is_rejected(self) -> None:
        for page, size in ((2, 100), (11, 10), (1, 101)):
            with self.subTest(page=page, size=size):
                with self.assertRaises(QueryRejected):
                    self._execute(params={"region": "全部"}, page=page, size=size)

    def test_execution_timeout_fails_waiter_and_owner(self) -> None:
        # 断言超时只影响本次请求：不写入缓存、不读取旧缓存。
        params = {"region": "西南"}
        self.query.invalidate_cache(self.template_no, reason="测试前清空")
        results = _run_concurrently(
            4, lambda: self._execute(params=params, page=1, size=10, delay_ms=3000)
        )
        self.assertTrue(all(isinstance(result, QueryExecutionTimeout) for result in results))
        self.assertEqual(self.query.find_by_ref(self.template_no)["缓存状态"], "已失效")
        self.assertFalse(query_cache.has_fresh_entries(self.template_no))

    def test_cache_key_includes_params_version_and_generation(self) -> None:
        first = self._execute(params={"region": "华东"}, page=1, size=5)
        second = self._execute(params={"region": "华南"}, page=1, size=5)
        self.assertNotEqual(first["缓存键"], second["缓存键"])
        self.assertEqual(first["缓存键组成"]["模板版本"], "v1")
        self.assertEqual(first["缓存键组成"]["数据集代次"], first["数据集代次"])
        self.assertEqual(first["缓存键组成"]["参数区"], '{"region":"华东"}')
        self.assertNotEqual(first["items"], second["items"])

    def test_same_params_and_page_are_consistent(self) -> None:
        first = self._execute(params={"region": "华东"}, page=1, size=5)
        second = self._execute(params={"region": "华东"}, page=1, size=5)
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(first["缓存键"], second["缓存键"])
        self.assertEqual(second["缓存状态"], "命中缓存")

    def test_dataset_refresh_invalidates_old_keys(self) -> None:
        before = self._execute(params={"region": "华东"}, page=1, size=5)
        refresh = self.dataset.refresh(self.dataset_no)
        self.assertIsNotNone(refresh)
        self.assertIn(self.template_no, refresh["失效模板"])
        self.assertGreater(refresh["数据集代次"], before["数据集代次"])
        after = self._execute(params={"region": "华东"}, page=1, size=5)
        self.assertNotEqual(after["缓存键"], before["缓存键"])
        self.assertEqual(after["缓存状态"], "未命中重算")
        self.assertNotEqual(after["items"], before["items"])

    def test_cache_expiry_does_not_replay_old_result(self) -> None:
        params = {"region": "东北"}
        entry = store.find("query", int(self.template["id"]))
        original_ttl = entry["缓存时间"]
        entry["缓存时间"] = 1
        try:
            first = self._execute(params=params, page=1, size=5)
            time.sleep(1.2)
            second = self._execute(params=params, page=1, size=5)
        finally:
            entry["缓存时间"] = original_ttl
        self.assertEqual(first["缓存键"], second["缓存键"])
        self.assertEqual(second["缓存状态"], "未命中重算")
        self.assertTrue(second["本次是否重算"])

    def test_generation_persists_across_list_reloads(self) -> None:
        self._execute(params={"region": "华东"}, page=1, size=5)
        self.dataset.refresh(self.dataset_no)
        first_listing = self.query.find_by_ref(self.template_no)
        second_listing = self.query.find_by_ref(self.template_no)
        self.assertEqual(first_listing["缓存代次"], second_listing["缓存代次"])
        self.assertEqual(first_listing["数据集代次"], second_listing["数据集代次"])
        self.assertEqual(first_listing["缓存状态"], second_listing["缓存状态"])

    def test_cache_rule_list_matches_query_generation_state(self) -> None:
        self._execute(params={"region": "华东"}, page=1, size=5)
        rule = self.cache.find_by_template(self.template_no)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["缓存代次"], self.query.find_by_ref(self.template_no)["缓存代次"])
        self.assertEqual(rule["失效状态"], "有效")
        self.dataset.refresh(self.dataset_no)
        rule = self.cache.find_by_template(self.template_no)
        template = self.query.find_by_ref(self.template_no)
        self.assertEqual(rule["缓存代次"], template["缓存代次"])
        self.assertEqual(rule["数据集代次"], template["数据集代次"])
        self.assertEqual(rule["失效状态"], "已失效")


if __name__ == "__main__":
    unittest.main()
