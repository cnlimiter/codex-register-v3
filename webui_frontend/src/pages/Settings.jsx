// pages/Settings.jsx
import { useState, useEffect, useCallback, useRef, createContext, useContext, useMemo } from 'react'
import api from '../lib/api.js'
import { Spinner } from '../components/Badge.jsx'

// ── Sticky save context ───────────────────────────────────────────────────
// Tabs register their save function here so the sticky header can call it.
const SaveCtx = createContext(null)

// ── Shared helpers ────────────────────────────────────────────────────────

function Section({ title, desc, children }) {
  return (
    <div className="border border-gray-100 rounded-xl overflow-hidden">
      <div className="bg-gray-50 px-5 py-3 border-b border-gray-100">
        <h4 className="text-sm font-semibold text-gray-700">{title}</h4>
        {desc && <p className="text-xs text-gray-400 mt-0.5">{desc}</p>}
      </div>
      <div className="p-5">{children}</div>
    </div>
  )
}

function Field({ label, hint, children }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 items-start py-2.5 border-b border-gray-50 last:border-0">
      <div className="sm:pt-2">
        <p className="text-sm font-medium text-gray-700">{label}</p>
        {hint && <p className="text-xs text-gray-400 mt-0.5">{hint}</p>}
      </div>
      <div className="sm:col-span-2">{children}</div>
    </div>
  )
}

function Input({ type = 'text', className = '', ...props }) {
  return (
    <input
      type={type}
      className={`block w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 ${className}`}
      {...props}
    />
  )
}

function Select({ options, ...props }) {
  return (
    <select
      className="block w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
      {...props}
    >
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  )
}

function Toggle({ checked, onChange }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${checked ? 'bg-blue-600' : 'bg-gray-200'}`}
    >
      <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-6' : 'translate-x-1'}`} />
    </button>
  )
}

function SaveBtn({ onClick, saving, saved, error }) {
  return (
    <div className="flex items-center gap-3 pt-4">
      <button
        onClick={onClick}
        disabled={saving}
        className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors"
      >
        {saving && <Spinner />}
        {saving ? '保存中…' : '保存'}
      </button>
      {saved && <span className="text-sm text-green-600">✓ 已保存</span>}
      {error && <span className="text-sm text-red-500">{error}</span>}
    </div>
  )
}

function useSave(isolated = false) {
  const rawCtx = useContext(SaveCtx)
  // isolated=true → do not sync with sticky header (e.g. per-section mail saves)
  const ctxRef = useRef(null)
  useEffect(() => { ctxRef.current = isolated ? null : rawCtx }, [rawCtx, isolated])

  const [saving, setSaving] = useState(false)
  const [saved, setSaved]   = useState(false)
  const [error, setError]   = useState('')

  // Stable run function (uses ref to avoid stale closures)
  const run = useCallback(async (fn) => {
    setSaving(true); setSaved(false); setError('')
    ctxRef.current?.onState({ saving: true, saved: false, error: '' })
    try {
      await fn()
      setSaved(true); setTimeout(() => setSaved(false), 2500)
      ctxRef.current?.onState({ saving: false, saved: true, error: '' })
      setTimeout(() => ctxRef.current?.onState({ saving: false, saved: false, error: '' }), 2500)
    } catch (e) {
      setError(e.message)
      ctxRef.current?.onState({ saving: false, saved: false, error: e.message })
    } finally { setSaving(false) }
  }, [])

  // Register the tab's save function with the sticky header
  const registerSave = useCallback((fn) => {
    ctxRef.current?.registerSave(fn)
  }, [])

  return { saving, saved, error, run, registerSave }
}

// ── Indeterminate Checkbox ────────────────────────────────────────────────

function IndeterminateCheckbox({ indeterminate, className = '', ...props }) {
  const ref = useRef(null)
  useEffect(() => { if (ref.current) ref.current.indeterminate = !!indeterminate }, [indeterminate])
  return <input type="checkbox" ref={ref} className={`rounded cursor-pointer accent-blue-600 ${className}`} {...props} />
}

// ── Import Modal ──────────────────────────────────────────────────────────

function ImportModal({ title, placeholder, hint, onParse, onImport, onClose }) {
  const [text,   setText]   = useState('')
  const [parsed, setParsed] = useState([])
  const [err,    setErr]    = useState('')
  const [saving, setSaving] = useState(false)

  const parse = async () => {
    setErr('')
    try {
      const result = await onParse(text)
      setParsed(result)
    } catch (e) {
      setErr(e.message || String(e))
      setParsed([])
    }
  }

  const confirm = async () => {
    setSaving(true)
    try { await onImport(parsed); onClose() }
    catch (e) { setErr(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl flex flex-col max-h-[90vh]">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h3 className="font-semibold text-gray-800">{title}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>

        <div className="p-6 flex-1 overflow-y-auto space-y-4">
          {hint && (
            <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 text-xs text-blue-700 whitespace-pre-wrap font-mono leading-relaxed">
              {hint}
            </div>
          )}
          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            placeholder={placeholder}
            rows={8}
            className="block w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-400"
          />
          <button
            onClick={parse}
            disabled={!text.trim()}
            className="bg-gray-100 hover:bg-gray-200 disabled:opacity-50 text-gray-700 text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            解析预览
          </button>

          {err && <p className="text-xs text-red-500">{err}</p>}

          {parsed.length > 0 && (
            <div>
              <p className="text-sm font-medium text-gray-700 mb-2">
                解析成功 <span className="text-blue-600 font-bold">{parsed.length}</span> 条
              </p>
              <div className="border border-gray-100 rounded-lg overflow-auto max-h-48">
                <table className="min-w-full text-xs">
                  <thead className="bg-gray-50">
                    <tr>
                      {Object.keys(parsed[0])
                        .filter(k => !['access_token','refresh_token','password'].includes(k))
                        .map(k => <th key={k} className="px-3 py-2 text-left text-gray-500 font-medium">{k}</th>)}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50">
                    {parsed.map((row, i) => (
                      <tr key={i}>
                        {Object.entries(row)
                          .filter(([k]) => !['access_token','refresh_token','password'].includes(k))
                          .map(([k, v]) => <td key={k} className="px-3 py-1.5 text-gray-600 font-mono">{String(v)}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-end gap-3">
          <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-700">取消</button>
          <button
            onClick={confirm}
            disabled={!parsed.length || saving}
            className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors"
          >
            {saving && <Spinner />}
            {saving ? '添加中…' : `添加 ${parsed.length} 条`}
          </button>
        </div>
      </div>
    </div>
  )
}

function useProviderOptions() {
  const [opts, setOpts] = useState([
    ['imap:0', 'IMAP 服务商 1'], ['gptmail', 'GptMail'],
  ])
  useEffect(() => {
    api.getSettings().then(s => {
      const items = []
      const imapProviders = Array.isArray(s['mail.imap']) ? s['mail.imap'] : []
      const isNewFormat = imapProviders.length > 0 && 'accounts' in imapProviders[0]
      if (isNewFormat) {
        imapProviders.forEach((prov, i) => {
          const name = prov.name || `IMAP 服务商 ${i + 1}`
          const accs = Array.isArray(prov.accounts) ? prov.accounts : []
          items.push([`imap:${i}`, `${name}（全部 ${accs.length} 账户轮换）`])
          accs.forEach((acc, j) => {
            items.push([`imap:${i}:${j}`, `└ ${acc.email || `账户 ${j + 1}`}`])
          })
        })
      } else {
        imapProviders.forEach((acc, i) =>
          items.push([`imap:${i}`, acc.email ? `IMAP: ${acc.email}` : `IMAP 账户 ${i + 1}`])
        )
      }
      if (items.filter(([v]) => v.startsWith('imap')).length === 0)
        items.push(['imap:0', 'IMAP 服务商 1'])
      const outlookAccounts = Array.isArray(s['mail.outlook']) ? s['mail.outlook'] : []
      if (outlookAccounts.length > 0) {
        items.push(['outlook', `Outlook（全部 ${outlookAccounts.length} 账户轮换）`])
        outlookAccounts.forEach((acc, i) => {
          items.push([`outlook:${i}`, `└ ${acc.email || `Outlook 账户 ${i + 1}`}`])
        })
      }
      items.push(['gptmail', 'GptMail'], ['npcmail', 'NpcMail'], ['yydsmail', 'YYDSMail'])
      setOpts(items)
    }).catch(() => {})
  }, [])
  return opts
}

// ── Tab: General ──────────────────────────────────────────────────────────

function TabGeneral() {
  const [cfg, setCfg] = useState(null)
  const { run, registerSave } = useSave()
  const providerOpts = useProviderOptions()

  useEffect(() => { api.getConfig().then(setCfg).catch(() => {}) }, [])
  const set = (k, v) => setCfg(c => ({ ...c, [k]: v }))

  const save = useCallback(() => run(async () => {
    await api.saveConfig({
      engine: cfg.engine, headless: cfg.headless, mobile: cfg.mobile,
      max_concurrent: cfg.max_concurrent, slow_mo: cfg.slow_mo,
      mail_provider: cfg.mail_provider, proxy_strategy: cfg.proxy_strategy,
      proxy_static: cfg.proxy_static ?? '',
    })
  }), [run, cfg])

  useEffect(() => { registerSave(save) }, [save, registerSave])

  if (!cfg) return <div className="py-8 text-center text-gray-400"><Spinner size="md" /></div>

  return (
    <div className="space-y-4">
      <Section title="浏览器配置">
        <Field label="引擎" hint="camoufox 可绕过 Turnstile">
          <Select value={cfg.engine} onChange={e => set('engine', e.target.value)}
            options={[['camoufox','Camoufox (Firefox 防指纹，推荐)'],['playwright','Playwright (Chromium)']]} />
        </Field>
        <Field label="无头模式"><Toggle checked={!!cfg.headless} onChange={v => set('headless', v)} /></Field>
        <Field label="手机模式"><Toggle checked={!!cfg.mobile} onChange={v => set('mobile', v)} /></Field>
        <Field label="慢速延迟 (ms)" hint="0 = 自动">
          <Input type="number" min={0} max={5000} value={cfg.slow_mo ?? 0} onChange={e => set('slow_mo', +e.target.value)} />
        </Field>
      </Section>
      <Section title="并发 & 邮件">
        <Field label="最大并发数">
          <Input type="number" min={1} max={20} value={cfg.max_concurrent ?? 2} onChange={e => set('max_concurrent', +e.target.value)} />
        </Field>
        <Field label="默认邮件服务商">
          <Select value={cfg.mail_provider ?? 'imap:0'} onChange={e => set('mail_provider', e.target.value)}
            options={providerOpts} />
        </Field>
      </Section>
      <Section title="代理配置">
        <Field label="代理策略">
          <Select value={cfg.proxy_strategy ?? 'none'} onChange={e => set('proxy_strategy', e.target.value)}
            options={[['none','不使用代理'],['static','固定代理'],['pool','代理池']]} />
        </Field>
        {cfg.proxy_strategy === 'static' && (
          <Field label="固定代理地址" hint="http://host:port">
            <Input value={cfg.proxy_static ?? ''} onChange={e => set('proxy_static', e.target.value)} placeholder="http://127.0.0.1:7890" />
          </Field>
        )}
      </Section>
    </div>
  )
}

// ── Tab: Mail API providers ───────────────────────────────────────────────

function MailProviderSection({ name, label }) {
  const [data, setData] = useState({ api_key: '', base_url: '' })
  const { saving, saved, error, run } = useSave(true)  // isolated — has its own save button
  useEffect(() => { api.getSection(`mail.${name}`).then(setData).catch(() => {}) }, [name])
  const save = () => run(() => api.saveSection(`mail.${name}`, data))
  return (
    <Section title={label}>
      <Field label="API Key">
        <Input type="password" value={data.api_key} onChange={e => setData(d => ({ ...d, api_key: e.target.value }))} placeholder="sk-..." />
      </Field>
      <Field label="Base URL">
        <Input value={data.base_url} onChange={e => setData(d => ({ ...d, base_url: e.target.value }))} />
      </Field>
      <SaveBtn onClick={save} saving={saving} saved={saved} error={error} />
    </Section>
  )
}

function TabMail() {
  return (
    <div className="space-y-4">
      <MailProviderSection name="gptmail"  label="GptMail" />
      <MailProviderSection name="npcmail"  label="NpcMail" />
      <MailProviderSection name="yydsmail" label="YYDSMail" />
    </div>
  )
}

// ── Tab: IMAP accounts (provider+accounts structure) ──────────────────────

const EMPTY_IMAP_PROVIDER = {
  name: '新 IMAP 服务商',
  host: '',
  port: 993,
  ssl: true,
  folder: 'INBOX',
  auth_type: 'password',
  use_alias: null,
  accounts: [],
}

const EMPTY_IMAP_ACCOUNT = { email: '', credential: '' }

const IMAP_ACCOUNT_IMPORT_HINT = `# 每行一个账户，格式: 邮箱<空格或TAB>密码/授权码
# 也支持四短线分隔：
user@qq.com  授权码1
user2@qq.com授权码2
user3@163.com----授权码3`.trim()

function ImapAccountImportModal({ onImport, onClose }) {
  const [text, setText] = useState('')
  const [parsed, setParsed] = useState([])
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)

  const parse = async () => {
    setErr('')
    try {
      const r = await api.parseImapAccountsNew(text)
      setParsed(r.parsed)
    } catch (e) {
      setErr(e.message)
      setParsed([])
    }
  }

  const confirm = async () => {
    setSaving(true)
    try { onImport(parsed); onClose() }
    catch (e) { setErr(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg flex flex-col max-h-[80vh]">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h3 className="font-semibold text-gray-800">批量导入账户</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>
        <div className="p-6 flex-1 overflow-y-auto space-y-3">
          <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 text-xs text-blue-700 font-mono whitespace-pre-wrap leading-relaxed">
            {IMAP_ACCOUNT_IMPORT_HINT}
          </div>
          <textarea
            value={text} onChange={e => setText(e.target.value)}
            placeholder="粘贴账户列表..."
            rows={6}
            className="block w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-400"
          />
          <button onClick={parse} disabled={!text.trim()}
            className="bg-gray-100 hover:bg-gray-200 disabled:opacity-50 text-gray-700 text-sm font-medium px-4 py-2 rounded-lg transition-colors">
            解析预览
          </button>
          {err && <p className="text-xs text-red-500">{err}</p>}
          {parsed.length > 0 && (
            <p className="text-sm text-gray-700">
              解析成功 <span className="text-blue-600 font-bold">{parsed.length}</span> 个账户
            </p>
          )}
        </div>
        <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-end gap-3">
          <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-700">取消</button>
          <button onClick={confirm} disabled={!parsed.length || saving}
            className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors">
            {saving && <Spinner />}
            {saving ? '添加中…' : `添加 ${parsed.length} 个账户`}
          </button>
        </div>
      </div>
    </div>
  )
}

function TabImap() {
  const [providers, setProviders] = useState([])
  const [importForIdx, setImportForIdx] = useState(null)
  const [collapsed, setCollapsed] = useState({})
  const [imapSel, setImapSel] = useState({})   // { [pi]: Set<accountIndex> }
  const { run, registerSave } = useSave()

  const load = useCallback(() => {
    api.getSection('mail.imap').then(d => {
      const raw = Array.isArray(d) ? d : []
      if (raw.length > 0 && !('accounts' in raw[0])) {
        setProviders([{
          ...EMPTY_IMAP_PROVIDER,
          name: '默认 IMAP 服务商',
          accounts: raw.map(a => ({ email: a.email || '', credential: a.password || a.access_token || '' })),
        }])
      } else {
        setProviders(raw)
      }
      setImapSel({})
    }).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const save = useCallback(() => run(() => api.saveSection('mail.imap', providers)), [run, providers])
  useEffect(() => { registerSave(save) }, [save, registerSave])

  // Provider helpers
  const addProvider    = () => setProviders(p => [...p, { ...EMPTY_IMAP_PROVIDER, accounts: [] }])
  const removeProvider = (pi) => { setProviders(p => p.filter((_, i) => i !== pi)); setImapSel(s => { const n={...s}; delete n[pi]; return n }) }
  const updateProvider = (pi, k, v) => setProviders(p => p.map((prov, i) => i === pi ? { ...prov, [k]: v } : prov))
  const toggleCollapse = (pi) => setCollapsed(c => ({ ...c, [pi]: !c[pi] }))

  // Account helpers
  const addAccount    = (pi) => setProviders(p => p.map((prov, i) => i === pi ? { ...prov, accounts: [...prov.accounts, { ...EMPTY_IMAP_ACCOUNT }] } : prov))
  const removeAccount = (pi, ai) => {
    setProviders(p => p.map((prov, i) => i === pi ? { ...prov, accounts: prov.accounts.filter((_, j) => j !== ai) } : prov))
    setImapSel(s => { const prev = s[pi] || new Set(); const next = new Set([...prev].filter(x => x !== ai).map(x => x > ai ? x - 1 : x)); return { ...s, [pi]: next } })
  }
  const updateAccount = (pi, ai, k, v) => setProviders(p => p.map((prov, i) =>
    i === pi ? { ...prov, accounts: prov.accounts.map((acc, j) => j === ai ? { ...acc, [k]: v } : acc) } : prov
  ))

  // Bulk selection helpers per provider
  const pSel     = (pi) => imapSel[pi] || new Set()
  const pAllSel  = (pi, accs) => accs.length > 0 && accs.every((_, ai) => pSel(pi).has(ai))
  const pSomeSel = (pi, accs) => !pAllSel(pi, accs) && accs.some((_, ai) => pSel(pi).has(ai))

  const toggleImapRow = (pi, ai) => setImapSel(s => {
    const prev = s[pi] || new Set(); const next = new Set(prev)
    next.has(ai) ? next.delete(ai) : next.add(ai)
    return { ...s, [pi]: next }
  })
  const toggleImapAll = (pi, accs) => setImapSel(s => {
    const prev = s[pi] || new Set()
    const all = pAllSel(pi, accs)
    const next = all ? new Set() : new Set(accs.map((_, ai) => ai))
    return { ...s, [pi]: next }
  })
  const deleteImapSelected = (pi) => {
    const selected = pSel(pi)
    if (selected.size === 0) return
    setProviders(p => p.map((prov, i) => i === pi ? { ...prov, accounts: prov.accounts.filter((_, ai) => !selected.has(ai)) } : prov))
    setImapSel(s => ({ ...s, [pi]: new Set() }))
  }

  const handleImportAccounts = (pi, parsed) => {
    setProviders(p => p.map((prov, i) => {
      if (i !== pi) return prov
      const existingEmails = new Set(prov.accounts.map(a => a.email.toLowerCase()))
      const newAccounts = parsed.filter(a => !existingEmails.has(a.email.toLowerCase()))
      return { ...prov, accounts: [...prov.accounts, ...newAccounts] }
    }))
  }

  const totalAccounts = providers.reduce((sum, p) => sum + (p.accounts?.length || 0), 0)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-600">
          共 <span className="font-semibold text-blue-600">{providers.length}</span> 个服务商，
          <span className="font-semibold text-blue-600 ml-1">{totalAccounts}</span> 个账户
        </p>
      </div>

      {providers.map((prov, pi) => {
        const isCollapsed = collapsed[pi]
        const accs = prov.accounts || []
        const accCount = accs.length
        const selSet = pSel(pi)
        const allSel = pAllSel(pi, accs)
        const someSel = pSomeSel(pi, accs)

        return (
          <div key={pi} className="border border-gray-200 rounded-xl overflow-hidden">
            {/* Provider header */}
            <div className="bg-gray-50 px-5 py-3 border-b border-gray-100 flex items-center justify-between">
              <div className="flex items-center gap-2 flex-1 min-w-0">
                <button onClick={() => toggleCollapse(pi)} className="text-gray-400 hover:text-gray-600 text-sm">
                  {isCollapsed ? '▶' : '▼'}
                </button>
                <input
                  value={prov.name || ''}
                  onChange={e => updateProvider(pi, 'name', e.target.value)}
                  className="text-sm font-semibold text-gray-700 bg-transparent border-0 outline-none focus:bg-white focus:border focus:border-gray-200 focus:rounded px-1 min-w-0 flex-1"
                  placeholder="服务商名称"
                />
                <span className="text-xs text-gray-400 whitespace-nowrap">{accCount} 账户</span>
              </div>
              <button onClick={() => removeProvider(pi)} className="text-xs text-red-400 hover:text-red-600 ml-3">删除服务商</button>
            </div>

            {!isCollapsed && (
              <div className="p-5 space-y-4">
                {/* Shared config */}
                <div className="space-y-0">
                  <Field label="IMAP 服务器" hint="留空则按账户域名自动检测">
                    <Input value={prov.host || ''} onChange={e => updateProvider(pi, 'host', e.target.value)} placeholder="imap.qq.com（可留空）" />
                  </Field>
                  <Field label="端口">
                    <Input type="number" value={prov.port || 993} onChange={e => updateProvider(pi, 'port', +e.target.value)} />
                  </Field>
                  <Field label="SSL/TLS"><Toggle checked={!!prov.ssl} onChange={v => updateProvider(pi, 'ssl', v)} /></Field>
                  <Field label="收件箱文件夹">
                    <Input value={prov.folder || 'INBOX'} onChange={e => updateProvider(pi, 'folder', e.target.value)} />
                  </Field>
                  <Field label="认证方式">
                    <Select value={prov.auth_type || 'password'} onChange={e => updateProvider(pi, 'auth_type', e.target.value)}
                      options={[['password','密码/授权码'],['oauth2','OAuth2 (XOAUTH2)']]} />
                  </Field>
                  <Field label="别名模式" hint="null=自动 (qq/gmail 自动启用 +alias)">
                    <Select
                      value={prov.use_alias === null || prov.use_alias === undefined ? 'auto' : prov.use_alias ? 'true' : 'false'}
                      onChange={e => updateProvider(pi, 'use_alias', e.target.value === 'auto' ? null : e.target.value === 'true')}
                      options={[['auto','自动 (qq/gmail 启用)'],['true','始终启用'],['false','始终禁用']]}
                    />
                  </Field>
                </div>

                {/* Accounts sub-list */}
                <div>
                  {/* Account list header with bulk select */}
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      {accCount > 0 && (
                        <IndeterminateCheckbox
                          checked={allSel}
                          indeterminate={someSel}
                          onChange={() => toggleImapAll(pi, accs)}
                        />
                      )}
                      <p className="text-xs font-medium text-gray-500 uppercase tracking-wider">
                        账户列表 ({accCount})
                      </p>
                      {selSet.size > 0 && (
                        <button
                          onClick={() => deleteImapSelected(pi)}
                          className="text-xs text-red-500 hover:text-red-700 bg-red-50 hover:bg-red-100 px-2 py-0.5 rounded transition-colors"
                        >
                          🗑️ 删除所选 {selSet.size} 个
                        </button>
                      )}
                    </div>
                    <button onClick={() => setImportForIdx(pi)}
                      className="text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 px-2.5 py-1 rounded transition-colors">
                      📥 批量导入
                    </button>
                  </div>

                  {accs.map((acc, ai) => {
                    const checked = selSet.has(ai)
                    return (
                      <div key={ai} className={`flex items-center gap-2 py-1.5 border-b border-gray-50 last:border-0 transition-colors ${checked ? 'bg-blue-50 -mx-1 px-1 rounded' : ''}`}>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleImapRow(pi, ai)}
                          className="rounded cursor-pointer accent-blue-600 flex-shrink-0"
                        />
                        <input
                          value={acc.email || ''}
                          onChange={e => updateAccount(pi, ai, 'email', e.target.value)}
                          placeholder="邮箱地址"
                          className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400"
                        />
                        <input
                          type="password"
                          value={acc.credential || ''}
                          onChange={e => updateAccount(pi, ai, 'credential', e.target.value)}
                          placeholder={prov.auth_type === 'oauth2' ? 'Access Token' : '密码/授权码'}
                          className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400"
                        />
                        <button onClick={() => removeAccount(pi, ai)} className="text-gray-300 hover:text-red-500 text-sm flex-shrink-0">×</button>
                      </div>
                    )
                  })}

                  <button onClick={() => addAccount(pi)}
                    className="mt-2 w-full border border-dashed border-gray-200 text-gray-400 hover:border-blue-300 hover:text-blue-500 rounded-lg py-1.5 text-xs transition-colors">
                    + 添加账户
                  </button>
                </div>
              </div>
            )}
          </div>
        )
      })}

      <button onClick={addProvider}
        className="w-full border-2 border-dashed border-gray-200 text-gray-400 hover:border-blue-300 hover:text-blue-500 rounded-xl py-3 text-sm transition-colors">
        + 添加 IMAP 服务商
      </button>

      {importForIdx !== null && (
        <ImapAccountImportModal
          onImport={(parsed) => handleImportAccounts(importForIdx, parsed)}
          onClose={() => setImportForIdx(null)}
        />
      )}
    </div>
  )
}

// ── Tab: Outlook / Hotmail ────────────────────────────────────────────────

const EMPTY_OUTLOOK = {
  email: '', password: '', client_id: '', tenant_id: 'consumers',
  refresh_token: '', access_token: '', fetch_method: 'graph', proxy: '',
}

const OUTLOOK_IMPORT_HINT = `# 四短线分隔（推荐，每行一条）：
邮箱----密码----Client Id----刷新令牌
user@outlook.com----MyPass----xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx----0.AXxx...

# JSON 数组格式：
[{"email":"user@outlook.com","client_id":"xxx","refresh_token":"0.AXxx..."}]

# 竖线分隔（每行一条）：
email|client_id|tenant_id|refresh_token[|fetch_method]`.trim()

// ── Outlook 账户编辑拟态框 ─────────────────────────────────────────────────

function OutlookEditModal({ account, onSave, onClose }) {
  const [acc, setAcc] = useState({ ...account })
  const set = (k, v) => setAcc(a => ({ ...a, [k]: v }))

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg flex flex-col max-h-[90vh]">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <div>
            <h3 className="font-semibold text-gray-800">Outlook 账户详情 / 编辑</h3>
            {acc.email && <p className="text-xs text-gray-400 mt-0.5">{acc.email}</p>}
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>
        <div className="p-6 flex-1 overflow-y-auto">
          <Field label="邮箱地址">
            <Input value={acc.email || ''} onChange={e => set('email', e.target.value)} placeholder="user@outlook.com" />
          </Field>
          <Field label="密码" hint="仅存储备用，OAuth 不使用">
            <Input type="password" value={acc.password || ''} onChange={e => set('password', e.target.value)} placeholder="（可选）" />
          </Field>
          <Field label="取件方式" hint="Graph API 推荐，无需 IMAP 权限">
            <Select value={acc.fetch_method || 'graph'} onChange={e => set('fetch_method', e.target.value)}
              options={[['graph','Microsoft Graph API（推荐）'],['imap','IMAP + XOAUTH2']]} />
          </Field>
          <Field label="Client ID" hint="Azure AD 应用程序 (客户端) ID">
            <Input value={acc.client_id || ''} onChange={e => set('client_id', e.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
          </Field>
          <Field label="Tenant ID" hint="个人账户: consumers | 企业: 租户 GUID 或 common">
            <Input value={acc.tenant_id || 'consumers'} onChange={e => set('tenant_id', e.target.value)} placeholder="consumers" />
          </Field>
          <Field label="Refresh Token" hint="长期有效，用于自动刷新 access_token">
            <Input type="password" value={acc.refresh_token || ''} onChange={e => set('refresh_token', e.target.value)} placeholder="0.AXxx..." />
          </Field>
          <Field label="Access Token" hint="可留空，系统自动获取">
            <Input type="password" value={acc.access_token || ''} onChange={e => set('access_token', e.target.value)} placeholder="（留空自动获取）" />
          </Field>
          <Field label="专用代理" hint="Graph/IMAP Token 刷新走此代理；留空则复用任务代理（大陆地区必须配置）">
            <Input value={acc.proxy || ''} onChange={e => set('proxy', e.target.value)}
              placeholder="http://127.0.0.1:10808（可选）" />
            {acc.proxy && (
              <p className="text-xs text-green-600 mt-1">✓ 已配置代理：{acc.proxy}</p>
            )}
            {!acc.proxy && (
              <p className="text-xs text-amber-500 mt-1">⚠ 未配置专用代理，将使用任务代理（如任务也无代理则直连）</p>
            )}
          </Field>
        </div>
        <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-end gap-3">
          <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-700">取消</button>
          <button
            onClick={() => { onSave(acc); onClose() }}
            className="bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors"
          >
            保存修改
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Outlook 批量导入拟态框（含取件方式选择）──────────────────────────────────

function OutlookImportModal({ onImport, onClose }) {
  const [text, setText] = useState('')
  const [fetchMethod, setFetchMethod] = useState('graph')
  const [parsed, setParsed] = useState([])
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)

  const parse = async () => {
    setErr('')
    try {
      const r = await api.parseOutlookAccounts(text)
      // 将选定的 fetch_method 应用到所有解析结果
      setParsed(r.parsed.map(a => ({ ...a, fetch_method: fetchMethod })))
    } catch (e) {
      setErr(e.message || String(e))
      setParsed([])
    }
  }

  // 切换方式时同步更新已解析列表
  const changeMethod = (method) => {
    setFetchMethod(method)
    setParsed(p => p.map(a => ({ ...a, fetch_method: method })))
  }

  const confirm = async () => {
    setSaving(true)
    try { await onImport(parsed); onClose() }
    catch (e) { setErr(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl flex flex-col max-h-[90vh]">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h3 className="font-semibold text-gray-800">批量导入 Outlook 账户</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>
        <div className="p-6 flex-1 overflow-y-auto space-y-4">

          {/* 取件方式选择 */}
          <div className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg">
            <span className="text-sm text-gray-600 font-medium whitespace-nowrap">取件方式：</span>
            <div className="flex gap-2">
              {[['graph','📊 Graph API（推荐）'], ['imap','📬 IMAP + XOAUTH2']].map(([v, l]) => (
                <button
                  key={v}
                  onClick={() => changeMethod(v)}
                  className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
                    fetchMethod === v
                      ? v === 'graph'
                        ? 'bg-green-600 text-white border-green-600 shadow-sm'
                        : 'bg-yellow-500 text-white border-yellow-500 shadow-sm'
                      : 'bg-white text-gray-600 border-gray-200 hover:border-gray-300'
                  }`}
                >
                  {l}
                </button>
              ))}
            </div>
            <span className="text-xs text-gray-400">将覆盖所有导入账户的取件方式</span>
          </div>

          {/* 格式提示 */}
          <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 text-xs text-blue-700 whitespace-pre-wrap font-mono leading-relaxed">
            {OUTLOOK_IMPORT_HINT}
          </div>

          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            placeholder="粘贴账户列表..."
            rows={8}
            className="block w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-400"
          />
          <button
            onClick={parse}
            disabled={!text.trim()}
            className="bg-gray-100 hover:bg-gray-200 disabled:opacity-50 text-gray-700 text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            解析预览
          </button>

          {err && <p className="text-xs text-red-500">{err}</p>}

          {parsed.length > 0 && (
            <div>
              <p className="text-sm font-medium text-gray-700 mb-2">
                解析成功 <span className="text-blue-600 font-bold">{parsed.length}</span> 条 &nbsp;·&nbsp; 取件方式：
                <span className={`ml-1 text-xs px-1.5 py-0.5 rounded font-semibold ${fetchMethod === 'graph' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}`}>
                  {fetchMethod}
                </span>
              </p>
              <div className="border border-gray-100 rounded-lg overflow-auto max-h-48">
                <table className="min-w-full text-xs">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-3 py-2 text-left text-gray-500 font-medium">email</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-medium">client_id</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-medium">fetch_method</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-medium">password</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50">
                    {parsed.map((row, i) => (
                      <tr key={i}>
                        <td className="px-3 py-1.5 text-gray-700 font-mono">{row.email}</td>
                        <td className="px-3 py-1.5 text-gray-500 font-mono">{row.client_id ? row.client_id.slice(0, 8) + '…' : '—'}</td>
                        <td className="px-3 py-1.5">
                          <span className={`text-xs px-1.5 py-0.5 rounded ${row.fetch_method === 'graph' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}`}>
                            {row.fetch_method}
                          </span>
                        </td>
                        <td className="px-3 py-1.5 text-gray-400">{row.password ? '******' : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
        <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-end gap-3">
          <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-700">取消</button>
          <button
            onClick={confirm}
            disabled={!parsed.length || saving}
            className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors"
          >
            {saving && <Spinner />}
            {saving ? '添加中…' : `添加 ${parsed.length} 条`}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Tab: Outlook ──────────────────────────────────────────────────────────

function TabOutlook() {
  const [accounts, setAccounts] = useState([])
  const [showImport, setShowImport] = useState(false)
  const [editIdx, setEditIdx] = useState(null)
  const [sel, setSel] = useState(new Set())    // Set<index>
  const { run, registerSave } = useSave()

  const load = useCallback(() => {
    api.getSection('mail.outlook').then(d => { setAccounts(Array.isArray(d) ? d : []); setSel(new Set()) }).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const add = () => {
    const idx = accounts.length
    setAccounts(a => [...a, { ...EMPTY_OUTLOOK }])
    setEditIdx(idx)
  }
  const remove = (i) => {
    setAccounts(a => a.filter((_, idx) => idx !== i))
    setSel(s => { const n = new Set([...s].filter(x => x !== i).map(x => x > i ? x - 1 : x)); return n })
    if (editIdx === i) setEditIdx(null)
  }
  const updateAcc = (i, updated) => setAccounts(a => a.map((acc, idx) => idx === i ? updated : acc))
  const save = useCallback(() => run(() => api.saveSection('mail.outlook', accounts)), [run, accounts])
  useEffect(() => { registerSave(save) }, [save, registerSave])
  const handleImport = async (parsed) => { await api.saveOutlookAccounts(parsed); load() }

  // Bulk selection
  const allSel  = accounts.length > 0 && accounts.every((_, i) => sel.has(i))
  const someSel = !allSel && accounts.some((_, i) => sel.has(i))

  const toggleRow = (i) => setSel(s => { const n = new Set(s); n.has(i) ? n.delete(i) : n.add(i); return n })
  const toggleAll = () => setSel(allSel ? new Set() : new Set(accounts.map((_, i) => i)))
  const clearSel  = () => setSel(new Set())

  const deleteSelected = () => {
    if (!sel.size) return
    if (!window.confirm(`确认删除 ${sel.size} 个 Outlook 账户？此操作在保存后生效。`)) return
    const indices = [...sel].sort((a, b) => b - a)  // delete from end to preserve indices
    let updated = [...accounts]
    indices.forEach(i => { updated.splice(i, 1) })
    setAccounts(updated)
    clearSel()
  }

  return (
    <div className="space-y-4">
      <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-800 space-y-1">
        <p className="font-semibold">📋 Outlook / Hotmail OAuth2 说明</p>
        <p>· 需在 Azure AD 注册应用并获取 client_id 和 refresh_token（设备码授权流程一次性获取）</p>
        <p>· <strong>Graph API（推荐）</strong>：权限 <code className="bg-blue-100 px-1 rounded">Mail.Read, offline_access</code></p>
        <p>· <strong>IMAP 方式</strong>：权限 <code className="bg-blue-100 px-1 rounded">IMAP.AccessAsUser.All, offline_access</code></p>
        <p>· tenant_id：个人账户填 <code className="bg-blue-100 px-1 rounded">consumers</code>，企业账户填租户 GUID 或 <code className="bg-blue-100 px-1 rounded">common</code></p>
        <p>· access_token 由系统自动刷新，无需手动填写</p>
      </div>

      {/* 工具栏 */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          {accounts.length > 0 && (
            <IndeterminateCheckbox
              checked={allSel}
              indeterminate={someSel}
              onChange={toggleAll}
            />
          )}
          <p className="text-sm text-gray-600">
            共 <span className="font-semibold text-blue-600">{accounts.length}</span> 个 Outlook 账户
          </p>
          {sel.size > 0 && (
            <button
              onClick={deleteSelected}
              className="text-xs text-red-500 hover:text-red-700 bg-red-50 hover:bg-red-100 px-2.5 py-1 rounded transition-colors"
            >
              🗑️ 删除所选 {sel.size} 个
            </button>
          )}
        </div>
        <button onClick={() => setShowImport(true)}
          className="text-sm bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-1.5 rounded-lg transition-colors">
          📥 批量导入
        </button>
      </div>

      {/* 账户卡片列表 */}
      <div className="space-y-2">
        {accounts.length === 0 && (
          <p className="text-center text-sm text-gray-400 py-6">暂无账户，点击下方按钮添加</p>
        )}
        {accounts.map((acc, i) => {
          const checked = sel.has(i)
          return (
            <div key={i}
              className={`border rounded-xl px-4 py-3 flex items-center gap-3 bg-white transition-colors ${checked ? 'border-blue-200 bg-blue-50' : 'border-gray-100 hover:border-gray-200'}`}
            >
              {/* 复选框 */}
              <input
                type="checkbox"
                checked={checked}
                onChange={() => toggleRow(i)}
                className="rounded cursor-pointer accent-blue-600 flex-shrink-0"
              />
              {/* 图标 */}
              <div className="w-8 h-8 rounded-full bg-blue-50 flex items-center justify-center flex-shrink-0 text-sm">
                🔵
              </div>
              {/* 信息 */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-800 truncate">
                    {acc.email || <span className="text-gray-400 font-normal">未设置邮箱</span>}
                  </span>
                  <span className={`text-xs px-1.5 py-0.5 rounded flex-shrink-0 ${
                    (acc.fetch_method || 'graph') === 'graph' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
                  }`}>
                    {acc.fetch_method || 'graph'}
                  </span>
                  {acc.proxy
                    ? <span className="text-xs px-1.5 py-0.5 rounded bg-purple-100 text-purple-700 flex-shrink-0" title={acc.proxy}>🔀 专用代理</span>
                    : <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-400 flex-shrink-0">直连/任务代理</span>
                  }
                </div>
                <p className="text-xs text-gray-400 mt-0.5 truncate">
                  {acc.client_id
                    ? <span className="font-mono">client: {acc.client_id.slice(0, 8)}…</span>
                    : <span className="text-orange-400">⚠ 未设置 Client ID</span>
                  }
                  {acc.refresh_token
                    ? <span className="ml-2 text-green-600">✓ Refresh Token</span>
                    : <span className="ml-2 text-orange-400">⚠ 未设置 Refresh Token</span>
                  }
                  {acc.password && <span className="ml-2">· 已设置密码</span>}
                </p>
              </div>
              {/* 操作 */}
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={() => setEditIdx(i)}
                  className="text-xs text-blue-500 hover:text-blue-700 px-2.5 py-1 rounded border border-blue-100 hover:border-blue-300 transition-colors"
                >
                  详情 / 编辑
                </button>
                <button onClick={() => remove(i)} className="text-xs text-red-400 hover:text-red-600 px-2 py-1">删除</button>
              </div>
            </div>
          )
        })}
      </div>

      <button onClick={add}
        className="w-full border-2 border-dashed border-gray-200 text-gray-400 hover:border-blue-300 hover:text-blue-500 rounded-xl py-3 text-sm transition-colors">
        + 手动添加 Outlook 账户
      </button>

      {showImport && (
        <OutlookImportModal onImport={handleImport} onClose={() => { setShowImport(false); load() }} />
      )}
      {editIdx !== null && accounts[editIdx] && (
        <OutlookEditModal
          account={accounts[editIdx]}
          onSave={(updated) => updateAcc(editIdx, updated)}
          onClose={() => setEditIdx(null)}
        />
      )}
    </div>
  )
}

// ── Tab: Timeouts ─────────────────────────────────────────────────────────

const TIMEOUT_LABELS = {
  page_load:            ['页面加载',       'page.goto() 超时'],
  auth0_redirect:       ['Auth0 重定向',   '等待跳转到 auth.openai.com'],
  email_input:          ['邮箱输入框',     '等待邮箱输入框出现'],
  password_input:       ['密码输入框',     '等待密码输入框出现'],
  otp_input:            ['OTP 输入框',     '等待 OTP 输入框出现'],
  otp_code:             ['OTP 验证码',     '轮询邮箱获取验证码'],
  profile_detect:       ['姓名页检测',     '等待姓名输入框出现'],
  profile_field:        ['姓名字段',       '等待每个字段'],
  complete_redirect:    ['注册完成跳转',   '等待跳回 chatgpt.com'],
  oauth_navigate:       ['OAuth 导航',     'page.goto() to /oauth/authorize'],
  oauth_flow_element:   ['OAuth 元素',     '等待授权按钮出现'],
  oauth_login_email:    ['OAuth 邮箱',     '等待 OAuth 重新登录邮箱框'],
  oauth_login_password: ['OAuth 密码',     '等待 OAuth 重新登录密码框'],
  oauth_token_exchange: ['Token 交换',     'httpx /oauth/token 超时'],
  oauth_total:          ['OAuth 总超时',   'OAuth 流程硬超时'],
}

function TabTimeouts() {
  const [data, setData] = useState({})
  const { run, registerSave } = useSave()
  useEffect(() => { api.getSection('timeouts').then(setData).catch(() => {}) }, [])
  const set  = (k, v) => setData(d => ({ ...d, [k]: v }))
  const save = useCallback(() => run(() => api.saveSection('timeouts', data)), [run, data])
  useEffect(() => { registerSave(save) }, [save, registerSave])
  return (
    <div className="space-y-4">
      <Section title="超时配置（单位：秒）">
        {Object.entries(TIMEOUT_LABELS).map(([k, [label, hint]]) => (
          <Field key={k} label={label} hint={hint}>
            <Input type="number" min={1} max={600} value={data[k] ?? ''} onChange={e => set(k, +e.target.value)} />
          </Field>
        ))}
      </Section>
    </div>
  )
}

// ── Tab: Advanced ─────────────────────────────────────────────────────────

function TabAdvanced() {
  const [mouse,  setMouse]  = useState({})
  const [timing, setTiming] = useState({})
  const [oauth,  setOauth]  = useState({ enabled: true, timeout: 45 })
  const [reg,    setReg]    = useState({ prefix: '', domain: '' })
  const [team,   setTeam]   = useState({ url: '', key: '' })
  const [sync,   setSync]   = useState({ url: '', key: '' })
  const { run, registerSave } = useSave()

  useEffect(() => {
    Promise.all([
      api.getSection('mouse').then(setMouse),
      api.getSection('timing').then(setTiming),
      api.getSection('oauth').then(setOauth),
      api.getSection('registration').then(setReg),
      api.getSection('team').then(setTeam),
      api.getSection('sync').then(setSync),
    ]).catch(() => {})
  }, [])

  const save = useCallback(() => run(async () => {
    await Promise.all([
      api.saveSection('mouse', mouse),
      api.saveSection('timing', timing),
      api.saveSection('oauth', oauth),
      api.saveSection('registration', reg),
      api.saveSection('team', team),
      api.saveSection('sync', sync),
    ])
  }), [run, mouse, timing, oauth, reg, team, sync])

  useEffect(() => { registerSave(save) }, [save, registerSave])

  return (
    <div className="space-y-4">
      <Section title="鼠标模拟配置">
        <Field label="人工模拟点击" hint="开启后模拟曲线鼠标轨迹，提升防检测；关闭则直接点击，速度更快">
          <div className="flex items-center gap-3">
            <Toggle
              checked={mouse.human_simulation !== false}
              onChange={v => setMouse(d => ({ ...d, human_simulation: v }))}
            />
            <span className={`text-xs px-2 py-0.5 rounded font-medium ${
              mouse.human_simulation !== false ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
            }`}>
              {mouse.human_simulation !== false ? '🖱️ 模拟轨迹' : '⚡ 直接点击'}
            </span>
          </div>
        </Field>
        <div className={mouse.human_simulation === false ? 'opacity-40 pointer-events-none select-none' : ''}>
          {[
            ['steps_min','最少弧线步数'],['steps_max','最多弧线步数'],
            ['step_delay_min','每步最短延迟 (秒)'],['step_delay_max','每步最长延迟 (秒)'],
            ['hover_min','悬停最短时间 (秒)'],['hover_max','悬停最长时间 (秒)'],
          ].map(([k, label]) => (
            <Field key={k} label={label}>
              <Input type="number" step="0.001" min={0} value={mouse[k] ?? ''} onChange={e => setMouse(d => ({ ...d, [k]: +e.target.value }))} />
            </Field>
          ))}
        </div>
      </Section>
      <Section title="等待时间配置 (秒)">
        {[
          ['post_nav','导航后等待','跳转/重定向后'],
          ['pre_fill','填写前等待','填写/点击前'],
          ['post_click','点击后等待','提交按钮后'],
          ['post_complete','完成后等待','COMPLETE 状态结束前'],
        ].map(([k, label, hint]) => (
          <Field key={k} label={label} hint={hint}>
            <Input type="number" step="0.1" min={0} value={timing[k] ?? ''} onChange={e => setTiming(d => ({ ...d, [k]: +e.target.value }))} />
          </Field>
        ))}
      </Section>
      <Section title="OAuth 配置">
        <Field label="自动获取 Token" hint="注册完成后自动完成 OAuth 授权">
          <Toggle checked={!!oauth.enabled} onChange={v => setOauth(d => ({ ...d, enabled: v }))} />
        </Field>
        <Field label="OAuth 超时 (秒)">
          <Input type="number" min={10} max={300} value={oauth.timeout ?? 45} onChange={e => setOauth(d => ({ ...d, timeout: +e.target.value }))} />
        </Field>
      </Section>
      <Section title="注册配置">
        <Field label="邮箱前缀"><Input value={reg.prefix ?? ''} onChange={e => setReg(d => ({ ...d, prefix: e.target.value }))} /></Field>
        <Field label="邮箱域名"><Input value={reg.domain ?? ''} onChange={e => setReg(d => ({ ...d, domain: e.target.value }))} /></Field>
      </Section>
      <Section title="团队 & 同步">
        <Field label="Team URL"><Input value={team.url ?? ''} onChange={e => setTeam(d => ({ ...d, url: e.target.value }))} placeholder="https://..." /></Field>
        <Field label="Team Key"><Input type="password" value={team.key ?? ''} onChange={e => setTeam(d => ({ ...d, key: e.target.value }))} /></Field>
        <Field label="Sync URL"><Input value={sync.url ?? ''} onChange={e => setSync(d => ({ ...d, url: e.target.value }))} placeholder="https://..." /></Field>
        <Field label="Sync Key"><Input type="password" value={sync.key ?? ''} onChange={e => setSync(d => ({ ...d, key: e.target.value }))} /></Field>
      </Section>
    </div>
  )
}

// ── Tab: Upload Config ────────────────────────────────────────────────────

const UPLOAD_PLATFORM_DEFS = {
  newapi: {
    label: 'NewAPI', icon: '🔌',
    credentialKey: 'api_key', credentialLabel: 'API Key', credentialPlaceholder: 'sk-...',
    urlPlaceholder: 'https://your-newapi-host',
    defaultConfig: { name: '', api_url: '', api_key: '', channel_type: 1, channel_models: '', channel_base_url: '' },
    extraFields: [
      { key: 'channel_type',     label: 'Channel Type', type: 'number', placeholder: '1' },
      { key: 'channel_base_url', label: 'Base URL',     type: 'text',   placeholder: '留空则使用默认' },
      { key: 'channel_models',   label: 'Models',       type: 'textarea',placeholder: '留空则使用默认模型列表，逗号分隔' },
    ],
  },
  cpa: {
    label: 'CPA', icon: '🛡️',
    credentialKey: 'api_token', credentialLabel: 'API Token', credentialPlaceholder: 'Bearer Token...',
    urlPlaceholder: 'https://your-cpa-host',
    defaultConfig: { name: '', api_url: '', api_token: '' },
    extraFields: [],
  },
  sub2api: {
    label: 'Sub2API', icon: '🚀',
    credentialKey: 'api_key', credentialLabel: 'API Key', credentialPlaceholder: 'Admin API Key',
    urlPlaceholder: 'https://your-sub2api-host',
    defaultConfig: { name: '', api_url: '', api_key: '', concurrency: 3, priority: 50 },
    extraFields: [
      { key: 'concurrency', label: '并发数',  type: 'number', placeholder: '3' },
      { key: 'priority',    label: '优先级',  type: 'number', placeholder: '50' },
    ],
  },
}

function UploadEndpointCard({ platform, cfg, onChange, onDelete }) {
  const def = UPLOAD_PLATFORM_DEFS[platform]
  const [expanded,   setExpanded]   = useState(!cfg.api_url)
  const [testing,    setTesting]    = useState(false)
  const [testResult, setTestResult] = useState(null)

  const set = (k, v) => onChange({ ...cfg, [k]: v })

  const handleTest = async () => {
    setTesting(true); setTestResult(null)
    try {
      const body = { platform, api_url: cfg.api_url, [def.credentialKey]: cfg[def.credentialKey] }
      const r = await api.testUploadConn(body)
      setTestResult(r)
    } catch (e) { setTestResult({ ok: false, message: e.message }) }
    finally { setTesting(false) }
  }

  const credValue = cfg[def.credentialKey] || ''
  const canTest   = !!(cfg.api_url && credValue)

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden mb-3">
      {/* Card header */}
      <div className="bg-gray-50 px-4 py-2.5 flex items-center gap-2">
        <button onClick={() => setExpanded(v => !v)} className="text-gray-400 hover:text-gray-600 text-xs flex-shrink-0">
          {expanded ? '▼' : '▶'}
        </button>
        <span className="text-base flex-shrink-0">{def.icon}</span>
        <input
          value={cfg.name || ''}
          onChange={e => set('name', e.target.value)}
          placeholder={`${def.label} 配置名称`}
          className="flex-1 min-w-0 text-sm font-medium text-gray-700 bg-transparent border-0 outline-none focus:bg-white focus:border focus:border-gray-200 focus:rounded px-1"
        />
        {cfg.api_url && (
          <span className="text-xs text-gray-400 truncate max-w-[180px] hidden sm:block">{cfg.api_url}</span>
        )}
        <button onClick={onDelete} className="text-xs text-red-400 hover:text-red-600 ml-2 flex-shrink-0">删除</button>
      </div>

      {expanded && (
        <div className="p-4 space-y-0">
          <Field label="API URL">
            <Input value={cfg.api_url || ''} onChange={e => set('api_url', e.target.value)} placeholder={def.urlPlaceholder} />
          </Field>
          <Field label={def.credentialLabel}>
            <Input type="password" value={credValue} onChange={e => set(def.credentialKey, e.target.value)} placeholder={def.credentialPlaceholder} />
          </Field>
          {def.extraFields.map(f => (
            <Field key={f.key} label={f.label}>
              {f.type === 'textarea' ? (
                <textarea
                  value={cfg[f.key] || ''}
                  onChange={e => set(f.key, e.target.value)}
                  placeholder={f.placeholder}
                  rows={2}
                  className="block w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-400"
                />
              ) : (
                <Input type={f.type} value={cfg[f.key] ?? ''} onChange={e => set(f.key, f.type === 'number' ? +e.target.value : e.target.value)} placeholder={f.placeholder} />
              )}
            </Field>
          ))}
          {/* Test row */}
          <div className="flex items-center gap-3 pt-3 border-t border-gray-50 mt-1">
            <button
              onClick={handleTest}
              disabled={testing || !canTest}
              className="text-xs border border-gray-200 bg-gray-50 hover:bg-gray-100 disabled:opacity-50 text-gray-600 px-3 py-1.5 rounded-lg transition-colors"
            >
              {testing ? '测试中…' : '🔍 测试连接'}
            </button>
            {testResult && (
              <span className={`text-xs font-medium ${testResult.ok ? 'text-green-600' : 'text-red-500'}`}>
                {testResult.ok ? '✓' : '✗'} {testResult.message}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function UploadPlatformSection({ platform, configs, onChange }) {
  const def = UPLOAD_PLATFORM_DEFS[platform]
  const add    = () => onChange([...configs, { ...def.defaultConfig }])
  const update = (i, v) => onChange(configs.map((c, j) => j === i ? v : c))
  const remove = (i) => onChange(configs.filter((_, j) => j !== i))
  return (
    <Section title={`${def.icon} ${def.label}`} desc={`可配置多个 ${def.label} 上传端点，账号管理页可多选上传`}>
      {configs.length === 0 && (
        <p className="text-sm text-gray-400 text-center py-2">暂无配置，点击下方按钮添加</p>
      )}
      {configs.map((cfg, i) => (
        <UploadEndpointCard
          key={i}
          platform={platform}
          cfg={cfg}
          onChange={v => update(i, v)}
          onDelete={() => remove(i)}
        />
      ))}
      <button
        onClick={add}
        className="w-full border-2 border-dashed border-gray-200 text-gray-400 hover:border-blue-300 hover:text-blue-500 rounded-xl py-2.5 text-sm transition-colors"
      >
        + 添加 {def.label} 端点
      </button>
    </Section>
  )
}

function TabUploadConfig() {
  const [configs, setConfigs] = useState({ newapi: [], cpa: [], sub2api: [] })
  const { run, registerSave } = useSave()

  useEffect(() => {
    Promise.all([
      api.getSection('upload.newapi'),
      api.getSection('upload.cpa'),
      api.getSection('upload.sub2api'),
    ]).then(([newapi, cpa, sub2api]) => {
      setConfigs({
        newapi:  Array.isArray(newapi)  ? newapi  : [],
        cpa:     Array.isArray(cpa)     ? cpa     : [],
        sub2api: Array.isArray(sub2api) ? sub2api : [],
      })
    }).catch(() => {})
  }, [])

  const save = useCallback(() => run(async () => {
    await Promise.all([
      api.saveSection('upload.newapi',  configs.newapi),
      api.saveSection('upload.cpa',     configs.cpa),
      api.saveSection('upload.sub2api', configs.sub2api),
    ])
  }), [run, configs])

  useEffect(() => { registerSave(save) }, [save, registerSave])

  const total = configs.newapi.length + configs.cpa.length + configs.sub2api.length

  return (
    <div className="space-y-4">
      <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-800 space-y-1">
        <p className="font-semibold">📤 上传平台配置说明</p>
        <p>· 每个平台可配置多个端点（多个网址），在账号管理页上传时可跨平台多选</p>
        <p>· 配置名称用于在账号管理的上传弹窗中识别端点</p>
        <p>· 使用「测试连接」验证 URL 和 Key 是否正确后再保存</p>
      </div>
      <p className="text-sm text-gray-500">
        共配置 <span className="font-semibold text-blue-600">{total}</span> 个端点
        <span className="ml-2 text-xs text-gray-400">（NewAPI: {configs.newapi.length}，CPA: {configs.cpa.length}，Sub2API: {configs.sub2api.length}）</span>
      </p>
      {['newapi', 'cpa', 'sub2api'].map(p => (
        <UploadPlatformSection
          key={p}
          platform={p}
          configs={configs[p]}
          onChange={updated => setConfigs(c => ({ ...c, [p]: updated }))}
        />
      ))}
    </div>
  )
}

// ── Main Settings page ─────────────────────────────────────────────────────

const TABS = [
  { key: 'general',   label: '⚙️ 通用配置' },
  { key: 'mail',      label: '📧 邮件服务' },
  { key: 'imap',      label: '📥 IMAP 账户' },
  { key: 'outlook',   label: '🔵 Outlook' },
  { key: 'timeouts',  label: '⏱ 超时设置' },
  { key: 'advanced',  label: '🔧 高级设置' },
  { key: 'upload',    label: '📤 上传配置' },
]

// Mail tab has per-section save buttons, so no global save button shown
const TABS_WITH_GLOBAL_SAVE = new Set(['general', 'imap', 'outlook', 'timeouts', 'advanced', 'upload'])

export function Settings() {
  const [tab, setTab] = useState('general')
  const [ctxState, setCtxState] = useState({ saving: false, saved: false, error: '' })
  const saveRef = useRef(null)

  // Stable context value — tabs register their save function here
  const ctxValue = useMemo(() => ({
    onState: setCtxState,
    registerSave: (fn) => { saveRef.current = fn },
  }), [])

  const handleTabChange = (key) => {
    setTab(key)
    saveRef.current = null
    setCtxState({ saving: false, saved: false, error: '' })
  }

  const showSaveBtn = TABS_WITH_GLOBAL_SAVE.has(tab)

  return (
    <SaveCtx.Provider value={ctxValue}>
      <div>
        {/* ── Sticky header: title + save button + tabs ── */}
        <div className="sticky top-0 z-20 bg-gray-50/95 backdrop-blur-sm border-b border-gray-200 px-6 pt-4 pb-0">
          <div className="flex items-start justify-between gap-4 mb-3">
            <div>
              <h2 className="text-xl font-bold text-gray-800">配置管理</h2>
              <p className="text-sm text-gray-500 mt-0.5">通用配置保存至 config.yaml，其余设置存入数据库</p>
            </div>
            <div className="flex items-center gap-3 pt-1 flex-shrink-0">
              {showSaveBtn ? (
                <>
                  <button
                    onClick={() => saveRef.current?.()}
                    disabled={ctxState.saving}
                    className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-medium px-5 py-2 rounded-lg transition-colors shadow-sm"
                  >
                    {ctxState.saving && <Spinner />}
                    {ctxState.saving ? '保存中…' : '保存'}
                  </button>
                  {ctxState.saved && <span className="text-sm text-green-600 font-medium">✓ 已保存</span>}
                  {ctxState.error && <span className="text-sm text-red-500 max-w-[200px] truncate">{ctxState.error}</span>}
                </>
              ) : (
                <span className="text-xs text-gray-400 italic">各服务商单独保存</span>
              )}
            </div>
          </div>

          {/* Tab bar */}
          <div className="flex gap-1 flex-wrap">
            {TABS.map(t => (
              <button key={t.key} onClick={() => handleTabChange(t.key)}
                className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap ${
                  tab === t.key
                    ? 'border-blue-600 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }`}>
                {t.label}
              </button>
            ))}
          </div>
        </div>

        {/* Tab content */}
        <div className="p-6 space-y-5">
          {tab === 'general'  && <TabGeneral />}
          {tab === 'mail'     && <TabMail />}
          {tab === 'imap'     && <TabImap />}
          {tab === 'outlook'  && <TabOutlook />}
          {tab === 'timeouts' && <TabTimeouts />}
          {tab === 'advanced' && <TabAdvanced />}
          {tab === 'upload'   && <TabUploadConfig />}
        </div>
      </div>
    </SaveCtx.Provider>
  )
}
