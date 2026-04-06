// src/lib/api.js — Thin wrapper around fetch for the FastAPI backend.
const BASE = '/api'

async function req(method, path, body) {
  const opts = { method }
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' }
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(BASE + path, opts)
  if (!res.ok) {
    let msg = `${res.status}`
    try { msg = (await res.json()).detail || msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json()
}

/** POST that returns a Blob (for file downloads via fetch). */
async function reqBlob(path, body) {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let msg = `${res.status}`
    try { msg = (await res.json()).detail || msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') || ''
  const match = cd.match(/filename=([^;]+)/)
  const filename = match ? match[1].replace(/"/g, '') : 'export'
  return { blob, filename }
}

const api = {
  // ── Common config (YAML-backed) ─────────────────────────────────────
  getConfig:  ()       => req('GET',  '/config'),
  saveConfig: (data)   => req('POST', '/config', data),

  // ── Non-common settings (DB-backed) ─────────────────────────────────
  getSettings:        ()            => req('GET',  '/settings'),
  getSection:         (s)           => req('GET',  `/settings/${encodeURIComponent(s)}`),
  saveSection:        (s, data)     => req('POST', `/settings/${encodeURIComponent(s)}`, data),
  getMergedConfig:    ()            => req('GET',  '/settings_merged'),

  // ── Mail import ──────────────────────────────────────────────────────────
  parseImapAccounts:    (text)     => req('POST', '/mail/import/imap',          { text }),
  parseImapAccountsNew: (text)     => req('POST', '/mail/import/imap/accounts', { text }),
  saveImapAccounts:     (accounts) => req('POST', '/mail/import/imap/save',     { accounts }),
  parseOutlookAccounts: (text)     => req('POST', '/mail/import/outlook',       { text }),
  saveOutlookAccounts:  (accounts) => req('POST', '/mail/import/outlook/save',  { accounts }),

  // ── Accounts ─────────────────────────────────────────────────────────
  getAccounts:        (params = {}) => req('GET',    '/accounts?' + new URLSearchParams(params)),
  getStats:           ()            => req('GET',    '/accounts/stats'),
  exportUrl:          (fmt)         => `${BASE}/accounts/export?fmt=${fmt}`,
  deleteAccount:      (email)       => req('DELETE', `/accounts/${encodeURIComponent(email)}`),
  batchDeleteAccounts:(body)        => req('POST',   '/accounts/batch-delete', body),

  /** Export selected accounts and trigger browser download. */
  exportSelected: async (body) => {
    const { blob, filename } = await reqBlob('/accounts/export-selected', body)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },

  // ── Upload to platforms ──────────────────────────────────────────────
  uploadNewapi:        (body) => req('POST', '/accounts/upload/newapi',  body),
  uploadCpa:           (body) => req('POST', '/accounts/upload/cpa',     body),
  uploadSub2api:       (body) => req('POST', '/accounts/upload/sub2api', body),
  testUploadConn:      (body) => req('POST', '/accounts/upload/test',    body),
  /** Upload to multiple configured endpoints at once. */
  batchUpload:         (body) => req('POST', '/accounts/upload/batch',   body),

  // ── Jobs ─────────────────────────────────────────────────────────────
  getJobs:         ()     => req('GET',    '/jobs'),
  getJob:          (id)   => req('GET',    `/jobs/${id}`),
  startJob:        (data) => req('POST',   '/jobs', data),
  cancelJob:       (id)   => req('POST',   `/jobs/${id}/cancel`),
  deleteJob:       (id)   => req('DELETE', `/jobs/${id}`),
  batchJobsAction: (body) => req('POST',   '/jobs/batch-action', body),

  // ── Proxies ──────────────────────────────────────────────────────────
  getProxies:         ()     => req('GET',    '/proxies'),
  addProxy:           (addr) => req('POST',   '/proxies', { address: addr }),
  deleteProxy:        (addr) => req('DELETE', `/proxies/${encodeURIComponent(addr)}`),
  batchDeleteProxies: (body) => req('POST',   '/proxies/batch-delete', body),
}

export default api

