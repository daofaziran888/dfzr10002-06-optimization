import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/query'
const columns = [
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
  "缓存状态"
]
const actions = [
  "提交审批",
  "发布模板",
  "停用模板"
]
const statuses = [
  "草稿",
  "待审批",
  "已发布",
  "已停用"
]
const stats = [
  {
    "label": "查询模板总数",
    "value": 0
  },
  {
    "label": "已发布模板",
    "value": 0
  },
  {
    "label": "待审批模板",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type ExecuteResult = {
  ok: boolean
  模板编号: string
  参数区: string
  缓存键: string
  缓存代次: number
  数据集代次: number
  缓存状态: string
  失效状态: string
  total: number
  返回条数: number
  page: number
  size: number
  结果上限: number
  已截断: boolean
  items: Record<string, string | number | null>[]
}

export default function QueryView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [region, setRegion] = useState('华东')
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(10)
  const [result, setResult] = useState<ExecuteResult | null>(null)
  const [executing, setExecuting] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('查询模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合查询配置列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function executeTemplate(row: Row) {
    setErrorMessage('')
    setExecuting(true)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/execute`, {
        method: 'POST',
        body: JSON.stringify({ params: { region }, page, size }),
      })
      const payload = await response.json()
      if (!response.ok) {
        // 超时、分页越界、代次冲突都走这里：旧缓存不会顶替新结果。
        throw new Error(String(payload?.detail ?? '查询执行失败'))
      }
      setResult(payload as ExecuteResult)
    } catch (error) {
      setResult(null)
      setErrorMessage(error instanceof Error ? error.message : '查询执行失败')
    } finally {
      setExecuting(false)
    }
  }

  async function refreshDataset(datasetNo: string) {
    setErrorMessage('')
    try {
      const response = await request(`/api/dataset/${datasetNo}/refresh`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(String(payload?.detail ?? '数据集刷新失败'))
      }
      setResult(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据集刷新失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('聚合查询配置动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合查询配置操作失败')
    }
  }

  return (
    <section className="page" data-module="query">
      <header className="page-head">
        <div>
          <h2>聚合查询配置管理</h2>
          <p className="page-desc">维护预定义的聚合查询模板、参数化查询与缓存策略，提升分析效率。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('查询模板登记入口尚未接入审批流')}>
            登记查询模板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出聚合查询配置清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>参数区 region</span>
          <input value={region} onChange={(event) => setRegion(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>页码</span>
          <input type="number" min={1} value={page} onChange={(event) => setPage(Number(event.target.value) || 1)} />
        </label>
        <label className="filter-item">
          <span>每页条数</span>
          <input type="number" min={1} value={size} onChange={(event) => setSize(Number(event.target.value) || 1)} />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" disabled={executing} onClick={() => void executeTemplate(row)}>
                  执行查询
                </button>
                <button className="link" type="button" onClick={() => void refreshDataset(String(row["数据集编号"] ?? ''))}>
                  刷新数据集
                </button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无聚合查询配置数据，可先登记查询模板</td>
            </tr>
          )}
        </tbody>
      </table>

      {result && (
        <article className="stat-card" style={{ marginTop: 12 }}>
          <div className="page-foot" style={{ flexWrap: 'wrap', gap: 12 }}>
            <span>模板 {result.模板编号}｜参数区 {result.参数区}｜缓存{result.缓存状态}｜缓存代次 {result.缓存代次}｜数据集代次 {result.数据集代次}｜失效状态 {result.失效状态}</span>
            <span>第 {result.page} 页 / 每页 {result.size} 条，结果上限 {result.结果上限}，已截断：{result.已截断 ? '是' : '否'}</span>
          </div>
          <p className="page-desc">缓存键 {result.缓存键}</p>
          <table className="data-table">
            <thead>
              <tr>
                {Object.keys(result.items[0] ?? {}).map((column) => <th key={column}>{column}</th>)}
              </tr>
            </thead>
            <tbody>
              {result.items.map((item, index) => (
                <tr key={index}>
                  {Object.keys(result.items[0] ?? {}).map((column) => <td key={column}>{item[column] ?? '—'}</td>)}
                </tr>
              ))}
              {result.items.length === 0 && (
                <tr><td className="empty-state" colSpan={5}>当前分页没有数据</td></tr>
              )}
            </tbody>
          </table>
          <p className="page-desc">共 {result.total} 条结果，本次返回 {result.返回条数} 条</p>
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条聚合查询配置记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
