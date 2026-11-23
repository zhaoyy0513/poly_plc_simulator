import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store'
import {
  PROTOCOL_META,
  type AiConfig,
  type AiDraft,
  type AiAnalyzeResult,
  type CreateServerBody,
  type Protocol,
  type Server,
  type Site,
} from '../types'
import { Icon } from './Icon'
import { Modal } from './Modal'

interface Props {
  open: boolean
  siteId: string
  site?: Site
  servers: Server[]
  onClose: () => void
}

interface Result {
  name: string
  ok: boolean
  error?: string
}

export function AiAnalyzeModal({ open, siteId, site, servers, onClose }: Props) {
  const notify = useApp((state) => state.notify)
  const createServer = useApp((state) => state.createServer)
  const fileInput = useRef<HTMLInputElement>(null)

  const [config, setConfig] = useState<AiConfig | null>(null)
  const [protocol, setProtocol] = useState<Protocol>('modbus_tcp')
  const [description, setDescription] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [items, setItems] = useState<AiDraft[]>([])
  const [results, setResults] = useState<Result[]>([])

  useEffect(() => {
    if (!open) return
    setError(null)
    setItems([])
    setWarnings([])
    setResults([])
    setDescription('')
    setFile(null)
    api
      .aiConfig()
      .then(setConfig)
      .catch(() => setConfig(null))
  }, [open])

  const analyze = async () => {
    if (!file) {
      setError('先选择 .xlsx 或 .csv 文件')
      return
    }
    setAnalyzing(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('site_id', siteId)
      form.append('protocol', protocol)
      form.append('description', description)
      form.append('file', file)
      const result: AiAnalyzeResult = await api.aiAnalyze(form)
      setItems(result.items)
      setWarnings(result.warnings)
      setResults([])
      if (!result.items.length) setError('AI 没有产出可用草稿，请调整描述后重试')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setAnalyzing(false)
    }
  }

  const update = (index: number, patch: Partial<AiDraft>) => {
    setItems((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  }

  const createSelected = async (indices: number[]) => {
    setCreating(true)
    setError(null)
    const next: Result[] = []
    for (const index of indices) {
      const draft = items[index]
      const body: CreateServerBody = {
        name: draft.name.trim(),
        kind: 'sim',
        source: 'ai',
        protocol: draft.protocol,
        bind_ip: draft.bind_ip || '0.0.0.0',
        port: Number(draft.port),
        options: draft.options,
        sample_tags: false,
        preset: draft.protocol === 'modbus_tcp' ? draft.preset : [],
      }
      try {
        await createServer(siteId, body)
        next.push({ name: body.name, ok: true })
      } catch (err) {
        next.push({ name: body.name, ok: false, error: err instanceof Error ? err.message : String(err) })
      }
    }
    setResults(next)
    const failed = next.filter((item) => !item.ok).length
    if (failed === 0) {
      notify('ok', `已创建 ${next.length} 个实例`)
      onClose()
    } else {
      notify('error', `${next.length - failed} 个创建成功，${failed} 个失败`)
    }
    setCreating(false)
  }

  const failedIndices = results.map((result, index) => (result.ok ? -1 : index)).filter((i) => i >= 0)
  const ready = items.length > 0 && items.every((item) => item.name.trim() && Number.isInteger(item.port))
  const disabled = !config?.configured || !config.enabled

  return (
    <Modal
      open={open}
      title="AI 点表分析"
      subtitle={
        disabled
          ? '请管理员先在「AI 配置」中完成 OpenAI 兼容端点配置并启用。'
          : '上传现场点表，用一句话说明要创建什么，AI 产出可编辑的实例草稿。'
      }
      width={760}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            关闭
          </button>
          {items.length > 0 ? (
            <button
              type="button"
              className="btn primary"
              disabled={creating || !ready}
              onClick={() => void createSelected(items.map((_, index) => index))}
            >
              {creating ? '创建中…' : `创建 ${items.length} 个实例`}
            </button>
          ) : null}
        </>
      }
    >
      <div className="field">
        <span>协议</span>
        <div className="protocol-pick">
          {(Object.keys(PROTOCOL_META) as Protocol[]).map((item) => (
            <button
              key={item}
              type="button"
              className={`pick${protocol === item ? ' active' : ''}`}
              onClick={() => setProtocol(item)}
            >
              <i style={{ background: PROTOCOL_META[item].accent }} />
              <strong>{PROTOCOL_META[item].label}</strong>
              <small>{PROTOCOL_META[item].hint}</small>
            </button>
          ))}
        </div>
      </div>

      <label className="field">
        <span>点表文件</span>
        <input
          ref={fileInput}
          className="input"
          type="file"
          accept=".xlsx,.csv"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <small className="hint">只读第一个 sheet、第一行表头；文件不超过 2MB。内容会发送给 AI 服务。</small>
      </label>

      <label className="field">
        <span>描述</span>
        <textarea
          className="input"
          rows={3}
          value={description}
          placeholder="例如：在此基础上创建 7 个实例，地址相同，起始地址分别偏移 0/100/200…"
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}

      <div className="modal-actions">
        <button type="button" className="btn ghost" disabled={analyzing || disabled || !file} onClick={() => void analyze()}>
          <Icon name="bolt" size={15} /> {analyzing ? '分析中…' : '分析'}
        </button>
      </div>

      {warnings.length ? (
        <div className="notice">
          <Icon name="alert" size={15} />
          <span>{warnings.join('；')}</span>
        </div>
      ) : null}

      {items.length ? (
        <div className="ai-drafts">
          <strong>草稿（可编辑名称与端口）</strong>
          <div className="ai-draft-head">
            <span>名称</span>
            <span>端口</span>
            <span>标签 / 预置</span>
          </div>
          {items.map((item, index) => (
            <div className="ai-draft-row" key={index}>
              <input
                className="input"
                value={item.name}
                onChange={(event) => update(index, { name: event.target.value })}
              />
              <input
                className="input mono"
                type="number"
                min={1}
                max={65535}
                value={item.port}
                onChange={(event) => update(index, { port: Number(event.target.value) })}
              />
              <span className="mono">
                {item.protocol === 'modbus_tcp'
                  ? `${item.preset.length} 个预置点`
                  : `${item.tags.length} 个标签`}
              </span>
            </div>
          ))}
          {results.length ? (
            <ul className="ai-results">
              {results.map((result, index) => (
                <li key={index} className={result.ok ? 'ok' : 'bad'}>
                  <Icon name={result.ok ? 'check' : 'alert'} size={14} />
                  <span className="mono">{result.name}</span>
                  {result.error ? <span>{result.error}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {failedIndices.length ? (
            <button
              type="button"
              className="btn ghost"
              disabled={creating}
              onClick={() => void createSelected(failedIndices)}
            >
              重试 {failedIndices.length} 个失败项
            </button>
          ) : null}
        </div>
      ) : null}
    </Modal>
  )
}
