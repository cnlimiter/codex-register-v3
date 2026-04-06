// pages/Accounts.jsx
import { useState, useEffect, useCallback, useRef } from 'react'
import api from '../lib/api.js'
import { StatusBadge } from '../components/Badge.jsx'

const STATUSES = ['', '注册完成', 'failed', 'email_creation_failed', 'imported']
const PAGE_SIZE = 50

function CopyBtn({ text }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(text ?? '').then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <button onClick={copy} title="复制" className="text-gray-400 hover:text-blue-500 transition-colors ml-1">
      {copied ? '✓' : '⎘'}
    </button>
  )
}

function IndeterminateCheckbox({ indeterminate, className = '', ...props }) {
  const ref = useRef(null)
  useEffect(() => { if (ref.current) ref.current.indeterminate = !!indeterminate }, [indeterminate])
  return <input type="checkbox" ref={ref} className={`rounded cursor-pointer accent-blue-600 ${className}`} {...props} />
}

// ── Upload Modal ──────────────────────────────────────────────────────────────

const PLATFORM_META = {
  newapi:  { icon: '🔌', label: 'NewAPI',  color: 'bg-blue-100 text-blue-700' },
  cpa:     { icon: '🛡️', label: 'CPA',     color: 'bg-purple-100 text-purple-700' },
  sub2api: { icon: '🚀', label: 'Sub2API', color: 'bg-green-100 text-green-700' },
}

function UploadModal({ emails, selAllDB, statusFilter, total, onClose }) {
  const [endpoints, setEndpoints] = useState([])   // [{platform, index, name, api_url, ...}]
  const [loadErr,   setLoadErr]   = useState('')
  const [loading,   setLoading]   = useState(true)
  const [selected,  setSelected]  = useState(new Set()) // Set<"platform:index">
  const [uploading, setUploading] = useState(false)
  const [results,   setResults]   = useState(null)   // {targets: [...]}
  const [error,     setError]     = useState('')
  const [expanded,  setExpanded]  = useState(new Set()) // expanded result cards

  const count = selAllDB ? total : emails.length

  // Load all configured endpoints on open
  useEffect(() => {
    Promise.all([
      api.getSection('upload.newapi'),
      api.getSection('upload.cpa'),
      api.getSection('upload.sub2api'),
    ]).then(([newapi, cpa, sub2api]) => {
      const all = []
      ;(Array.isArray(newapi)  ? newapi  : []).forEach((cfg, i) => all.push({ platform: 'newapi',  index: i, ...cfg }))
      ;(Array.isArray(cpa)     ? cpa     : []).forEach((cfg, i) => all.push({ platform: 'cpa',     index: i, ...cfg }))
      ;(Array.isArray(sub2api) ? sub2api : []).forEach((cfg, i) => all.push({ platform: 'sub2api', index: i, ...cfg }))
      setEndpoints(all)
    }).catch(e => setLoadErr(e.message)).finally(() => setLoading(false))
  }, [])

  const epKey    = (ep) => `${ep.platform}:${ep.index}`
  const toggle   = (key) => setSelected(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n })
  const toggleAll = () => {
    if (selected.size === endpoints.length) setSelected(new Set())
    else setSelected(new Set(endpoints.map(epKey)))
  }

  const handleUpload = async () => {
    if (!selected.size) { setError('请至少选择一个上传目标'); return }
    setUploading(true); setError(''); setResults(null)
    try {
      const targets = [...selected].map(key => {
        const [platform, idx] = key.split(':')
        return { platform, index: parseInt(idx) }
      })
      const r = await api.batchUpload({
        emails:     selAllDB ? [] : emails,
        select_all: selAllDB,
        status:     selAllDB ? statusFilter : '',
        targets,
      })
      setResults(r)
    } catch (e) { setError(e.message) }
    finally { setUploading(false) }
  }

  const toggleExpand = (key) => setExpanded(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n })

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg flex flex-col max-h-[92vh]">

        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <div>
            <h3 className="font-semibold text-gray-800">📤 上传到平台</h3>
            <p className="text-xs text-gray-400 mt-0.5">
              共 <span className="text-blue-600 font-bold">{count}</span> 个账号 · 可选多个目标端点
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>

        <div className="p-6 flex-1 overflow-y-auto space-y-4">

          {/* Loading */}
          {loading && (
            <div className="flex items-center justify-center py-8 gap-2 text-gray-400 text-sm">
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/>
              </svg>
              加载端点配置中…
            </div>
          )}

          {/* Load error */}
          {loadErr && <p className="text-xs text-red-500">{loadErr}</p>}

          {/* No endpoints configured */}
          {!loading && endpoints.length === 0 && (
            <div className="text-center py-8 space-y-2">
              <p className="text-2xl">📭</p>
              <p className="text-sm font-medium text-gray-700">暂无上传端点</p>
              <p className="text-xs text-gray-400">请先在「配置管理 → 📤 上传配置」中添加平台配置</p>
            </div>
          )}

          {/* Endpoint list */}
          {!loading && endpoints.length > 0 && !results && (
            <div className="space-y-2">
              {/* Select-all bar */}
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">选择上传目标（可多选）</p>
                <button onClick={toggleAll} className="text-xs text-blue-500 hover:text-blue-700 underline">
                  {selected.size === endpoints.length ? '取消全选' : `全选 (${endpoints.length})`}
                </button>
              </div>

              {endpoints.map(ep => {
                const key = epKey(ep)
                const checked = selected.has(key)
                const meta = PLATFORM_META[ep.platform] || { icon: '?', label: ep.platform, color: 'bg-gray-100 text-gray-600' }
                return (
                  <label
                    key={key}
                    className={`flex items-center gap-3 p-3 rounded-xl border-2 cursor-pointer transition-colors ${
                      checked ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-300'
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(key)}
                      className="accent-blue-600 flex-shrink-0"
                    />
                    <span className="text-lg flex-shrink-0">{meta.icon}</span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-medium text-gray-800 truncate">
                          {ep.name || `${meta.label} #${ep.index + 1}`}
                        </span>
                        <span className={`text-xs px-1.5 py-0.5 rounded font-medium flex-shrink-0 ${meta.color}`}>
                          {meta.label}
                        </span>
                      </div>
                      <p className="text-xs text-gray-400 truncate mt-0.5">{ep.api_url || '(未设置 URL)'}</p>
                    </div>
                  </label>
                )
              })}
            </div>
          )}

          {/* Error */}
          {error && (
            <p className="text-xs text-red-500 bg-red-50 border border-red-100 rounded-lg px-3 py-2">{error}</p>
          )}

          {/* Results */}
          {results && (
            <div className="space-y-3">
              {/* Summary */}
              {(() => {
                const succ  = results.targets.reduce((s, t) => s + (t.success_count || 0), 0)
                const skip  = results.targets.reduce((s, t) => s + (t.skipped_count || 0), 0)
                const fail  = results.targets.reduce((s, t) => s + (t.failed_count  || 0), 0)
                return (
                  <div className="flex items-center gap-4 bg-gray-50 rounded-xl px-4 py-3 text-sm">
                    <span className="text-green-600 font-semibold">✓ {succ} 成功</span>
                    <span className="text-amber-500 font-semibold">⊘ {skip} 跳过</span>
                    <span className="text-red-500 font-semibold">✗ {fail} 失败</span>
                    <span className="text-gray-400 text-xs ml-auto">{results.targets.length} 个端点</span>
                  </div>
                )
              })()}

              {/* Per-target result cards */}
              {results.targets.map((t, i) => {
                const key = `${t.platform}:${t.index}`
                const isExpanded = expanded.has(key)
                const meta = PLATFORM_META[t.platform] || { icon: '?', label: t.platform, color: 'bg-gray-100 text-gray-600' }
                return (
                  <div key={i} className="border border-gray-100 rounded-xl overflow-hidden">
                    <button
                      onClick={() => toggleExpand(key)}
                      className="w-full flex items-center gap-3 px-4 py-3 bg-gray-50 hover:bg-gray-100 transition-colors text-left"
                    >
                      <span className="text-base flex-shrink-0">{meta.icon}</span>
                      <div className="flex-1 min-w-0">
                        <span className="text-sm font-medium text-gray-800">{t.name}</span>
                        <span className={`ml-2 text-xs px-1.5 py-0.5 rounded ${meta.color}`}>{meta.label}</span>
                      </div>
                      {t.error ? (
                        <span className="text-xs text-red-500 flex-shrink-0">✗ {t.error}</span>
                      ) : (
                        <div className="flex items-center gap-2 text-xs flex-shrink-0">
                          <span className="text-green-600">✓{t.success_count}</span>
                          <span className="text-amber-500">⊘{t.skipped_count}</span>
                          <span className="text-red-500">✗{t.failed_count}</span>
                        </div>
                      )}
                      <span className="text-gray-400 text-xs">{isExpanded ? '▲' : '▼'}</span>
                    </button>
                    {isExpanded && t.details && t.details.length > 0 && (
                      <div className="max-h-40 overflow-y-auto divide-y divide-gray-50">
                        {t.details.map((d, j) => (
                          <div key={j} className={`flex items-center justify-between px-4 py-1.5 text-xs ${d.success ? '' : 'bg-red-50/40'}`}>
                            <span className="font-mono text-gray-600 truncate flex-1 min-w-0">{d.email}</span>
                            <span className={`ml-3 flex-shrink-0 ${d.success ? 'text-green-600' : 'text-red-500'}`}>
                              {d.success ? '✓' : `✗ ${d.error || ''}`}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-between gap-3">
          <span className="text-xs text-gray-400">
            {!results && selected.size > 0 && `已选 ${selected.size} 个端点`}
          </span>
          <div className="flex items-center gap-3">
            <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-700">
              {results ? '关闭' : '取消'}
            </button>
            {!results && (
              <button
                onClick={handleUpload}
                disabled={uploading || !selected.size || loading}
                className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors"
              >
                {uploading ? (
                  <>
                    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/>
                    </svg>
                    上传中…
                  </>
                ) : `📤 上传到 ${selected.size} 个端点`}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Export dropdown ─────────────────────────────────────────────────────────

function ExportDropdown({ emails, selAllDB, statusFilter, total }) {
  const [open, setOpen]     = useState(false)
  const [loading, setLoading] = useState(false)
  const count = selAllDB ? total : emails.length

  const doExport = async (fmt) => {
    setOpen(false)
    setLoading(true)
    try {
      await api.exportSelected({
        emails:     selAllDB ? [] : emails,
        select_all: selAllDB,
        status:     selAllDB ? statusFilter : '',
        fmt,
      })
    } catch (e) { alert('导出失败：' + e.message) }
    finally { setLoading(false) }
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(v => !v)}
        disabled={loading}
        className="flex items-center gap-1 text-xs bg-gray-700 hover:bg-gray-600 disabled:opacity-60 text-white px-3 py-1.5 rounded-lg transition-colors"
      >
        {loading ? '导出中…' : `⬇️ 导出所选 (${count})`}
        <span className="text-gray-400">▾</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full mb-2 right-0 z-50 bg-white border border-gray-200 rounded-xl shadow-xl overflow-hidden min-w-[130px]">
            <button onClick={() => doExport('json')} className="block w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 transition-colors">
              📄 导出 JSON
            </button>
            <button onClick={() => doExport('csv')} className="block w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 transition-colors border-t border-gray-100">
              📊 导出 CSV
            </button>
          </div>
        </>
      )}
    </div>
  )
}

// ── BulkBar ─────────────────────────────────────────────────────────────────

function BulkBar({ selCount, total, selAllDB, onSelectAllDB, onClearSel, children }) {
  if (selCount === 0 && !selAllDB) return null
  const displayCount = selAllDB ? total : selCount
  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-gray-900 text-white rounded-2xl shadow-2xl px-5 py-3 text-sm whitespace-nowrap">
      <span className="font-medium">
        已选 <span className="text-blue-400 font-bold">{displayCount}</span> 条
        {selAllDB && <span className="ml-1 text-xs text-green-400 font-semibold">（全库）</span>}
      </span>
      {!selAllDB && selCount > 0 && total > selCount && onSelectAllDB && (
        <button onClick={onSelectAllDB} className="text-xs text-blue-400 hover:text-blue-300 underline">
          选择全部 {total} 条
        </button>
      )}
      <div className="w-px h-4 bg-gray-700 flex-shrink-0" />
      {children}
      <button onClick={onClearSel} className="text-gray-500 hover:text-white text-lg leading-none ml-1">×</button>
    </div>
  )
}

export function Accounts() {
  const [rows, setRows]       = useState([])
  const [total, setTotal]     = useState(0)
  const [status, setStatus]   = useState('')
  const [page, setPage]       = useState(0)
  const [loading, setLoading] = useState(false)
  const [sel, setSel]           = useState(new Set())
  const [selAllDB, setSelAllDB] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [showUpload, setShowUpload] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    api.getAccounts({ status, limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      .then(d => { setRows(d.items); setTotal(d.total) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [status, page])

  useEffect(() => { load() }, [load])
  useEffect(() => { setSel(new Set()); setSelAllDB(false) }, [status, page])

  const pages       = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const pageEmails  = rows.map(r => r.email)
  const allPageSel  = pageEmails.length > 0 && pageEmails.every(e => sel.has(e))
  const somePageSel = !allPageSel && pageEmails.some(e => sel.has(e))

  const toggleRow  = (email) => { setSelAllDB(false); setSel(s => { const n = new Set(s); n.has(email) ? n.delete(email) : n.add(email); return n }) }
  const togglePage = () => { setSelAllDB(false); setSel(s => { const n = new Set(s); if (allPageSel) pageEmails.forEach(e => n.delete(e)); else pageEmails.forEach(e => n.add(e)); return n }) }
  const clearSel   = () => { setSel(new Set()); setSelAllDB(false) }

  const handleDelete = async () => {
    const n = selAllDB ? total : sel.size
    if (!window.confirm(`确认删除 ${n} 条账户记录？此操作不可撤销。`)) return
    setDeleting(true)
    try {
      await api.batchDeleteAccounts(selAllDB ? { select_all: true, status } : { emails: [...sel] })
      clearSel(); load()
    } catch (e) { alert('删除失败：' + e.message) }
    finally { setDeleting(false) }
  }

  const selEmails = [...sel]

  return (
    <div className="p-6 space-y-4 pb-24">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-bold text-gray-800">账户列表</h2>
          <p className="text-sm text-gray-500 mt-0.5">共 {total} 条记录</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <select value={status} onChange={e => { setStatus(e.target.value); setPage(0) }}
            className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
            {STATUSES.map(s => <option key={s} value={s}>{s || '全部状态'}</option>)}
          </select>
          <a href={api.exportUrl('csv')} className="bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-50 transition-colors">导出 CSV</a>
          <a href={api.exportUrl('json')} className="bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-50 transition-colors">导出 JSON</a>
          <button onClick={load} className="bg-blue-600 hover:bg-blue-700 text-white rounded-lg px-3 py-2 text-sm font-medium transition-colors">刷新</button>
        </div>
      </div>

      {/* 全库选择提示 */}
      {allPageSel && !selAllDB && total > rows.length && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-2.5 flex items-center gap-3 text-sm">
          <span className="text-blue-700">当前页 {rows.length} 条已全选。</span>
          <button onClick={() => setSelAllDB(true)} className="text-blue-600 hover:text-blue-800 font-semibold underline text-xs">
            选择全部数据库 {total} 条记录
          </button>
        </div>
      )}
      {selAllDB && (
        <div className="bg-green-50 border border-green-200 rounded-lg px-4 py-2.5 flex items-center gap-3 text-sm">
          <span className="text-green-700">已选中全部数据库 <strong>{total}</strong> 条记录。</span>
          <button onClick={clearSel} className="text-green-600 hover:text-green-800 underline text-xs">取消全选</button>
        </div>
      )}

      <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="border-b border-gray-100 bg-gray-50">
              <th className="px-4 py-3 w-10">
                <IndeterminateCheckbox
                  checked={allPageSel || selAllDB}
                  indeterminate={somePageSel && !selAllDB}
                  onChange={togglePage}
                />
              </th>
              {['邮箱', '密码', '状态', '服务商', 'Access Token', '注册时间'].map(h => (
                <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {loading && rows.length === 0 && <tr><td colSpan={7} className="text-center py-12 text-gray-400">加载中…</td></tr>}
            {!loading && rows.length === 0 && <tr><td colSpan={7} className="text-center py-12 text-gray-400">暂无数据</td></tr>}
            {rows.map(r => {
              const checked = selAllDB || sel.has(r.email)
              return (
                <tr key={r.email} className={`transition-colors ${checked ? 'bg-blue-50' : 'hover:bg-gray-50'}`}>
                  <td className="px-4 py-3 w-10">
                    <input type="checkbox" checked={checked} onChange={() => toggleRow(r.email)} className="rounded cursor-pointer accent-blue-600" />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-700 whitespace-nowrap">{r.email}<CopyBtn text={r.email} /></td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-500 whitespace-nowrap"><span className="select-all">{r.password}</span><CopyBtn text={r.password} /></td>
                  <td className="px-4 py-3"><StatusBadge status={r.status} /></td>
                  <td className="px-4 py-3 text-xs text-gray-500">{r.provider || '—'}</td>
                  <td className="px-4 py-3 max-w-[180px]">
                    {r.access_token
                      ? <div className="flex items-center gap-1"><span className="font-mono text-xs text-gray-400 truncate">{r.access_token.slice(0, 20)}…</span><CopyBtn text={r.access_token} /></div>
                      : <span className="text-gray-300 text-xs">—</span>}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-400 whitespace-nowrap">{r.created_at?.slice(0, 19) || '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-gray-500">第 {page + 1} / {pages} 页 · 共 {total} 条</span>
          <div className="flex gap-1">
            <button disabled={page === 0} onClick={() => setPage(p => p - 1)} className="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50">上一页</button>
            <button disabled={page >= pages - 1} onClick={() => setPage(p => p + 1)} className="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50">下一页</button>
          </div>
        </div>
      )}

      <BulkBar selCount={sel.size} total={total} selAllDB={selAllDB} onSelectAllDB={() => setSelAllDB(true)} onClearSel={clearSel}>
        {/* Export selected */}
        <ExportDropdown
          emails={selEmails}
          selAllDB={selAllDB}
          statusFilter={status}
          total={total}
        />
        {/* Upload to platform */}
        <button
          onClick={() => setShowUpload(true)}
          className="flex items-center gap-1.5 bg-indigo-500 hover:bg-indigo-600 text-white text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
        >
          📤 上传到平台
        </button>
        {/* Delete */}
        <button onClick={handleDelete} disabled={deleting}
          className="flex items-center gap-1.5 bg-red-500 hover:bg-red-600 disabled:opacity-60 text-white text-xs font-medium px-3 py-1.5 rounded-lg transition-colors">
          {deleting ? '删除中…' : '🗑️ 删除所选'}
        </button>
      </BulkBar>

      {showUpload && (
        <UploadModal
          emails={selEmails}
          selAllDB={selAllDB}
          statusFilter={status}
          total={total}
          onClose={() => setShowUpload(false)}
        />
      )}
    </div>
  )
}
