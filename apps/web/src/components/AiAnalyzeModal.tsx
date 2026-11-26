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

const DEFAULT_DESCRIPTION: Record<Protocol, string> = {
  modbus_tcp: '分析如下 Modbus 点表，创建对应的 Modbus Server 实例，端口随机选择，不要与现有本地 Modbus 端口冲突。',
  s7: '分析如下 S7 点表，创建对应的 S7 Server 实例，端口随机选择，不要与现有本地 S7 端口冲突。',
  opcua: '分析如下 OPC UA 点表，创建对应的 OPC UA Server 实例，端口随机选择，不要与现有本地 OPC UA 端口冲突。',
}

function isDefaultDescription(text: string): boolean {
  return Object.values(DEFAULT_DESCRIPTION).includes(text)
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
    setDescription(DEFAULT_DESCRIPTION[protocol])
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

  const removeItem = (index: number) => {
    setItems((prev) => prev.filter((_, i) => i !== index))
    setResults([])
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
              onClick={() => {
                setProtocol(item)
                if (isDefaultDescription(description)) {
                  setDescription(DEFAULT_DESCRIPTION[item])
                }
              }}
            >
              <i style={{ background: PROTOCOL_META[item].accent }} />
              <strong>{PROTOCOL_META[item].label}</strong>
              <small>{PROTOCOL_META[item].hint}</small>
            </button>
          ))}
        </div>
      </div>

      {config?.configured ? (
        <p className="hint">
          当前模型：<span className="mono">{config.model || '未设置'}</span>
        </p>
      ) : null}

      <label className="field">
        <span>点表文件</span>
        <div
          className="file-drop"
          onClick={() => fileInput.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              fileInput.current?.click()
            }
          }}
        >
          <Icon name="plus" size={16} />
          <span className={`file-drop-text${file ? '' : ' empty'}`}>
            {file ? file.name : '点击选择 .xlsx / .csv'}
          </span>
        </div>
        <input
          ref={fileInput}
          type="file"
          accept=".xlsx,.csv"
          hidden
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <small className="hint">支持 .xlsx / .csv，可包含多个 sheet；内容会发送给 AI 服务。</small>
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
            <span />
          </div>
          {items.map((item, index) => (
            <div className="ai-draft" key={index}>
              <div className="ai-draft-row">
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
                    ? `${item.preset.length} 个预置点${item.options?.register_count ? ` · 有效 0–${item.options.register_count - 1}` : ''}`
                    : `${item.tags.length} 个标签`}
                </span>
                <button
                  type="button"
                  className="icon-btn danger"
                  title="删除这个草稿"
                  onClick={() => removeItem(index)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
              {item.protocol === 'modbus_tcp' ? (
                <details className="ai-draft-detail">
                  <summary>预置点明细</summary>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>数据区</th>
                        <th>地址</th>
                        <th>值</th>
                      </tr>
                    </thead>
                    <tbody>
                      {item.preset.map((row, rowIndex) => (
                        <tr key={rowIndex}>
                          <td>{row.area}</td>
                          <td className="mono">{row.address}</td>
                          <td className="mono">{row.value}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              ) : (
                <details className="ai-draft-detail">
                  <summary>标签明细</summary>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>名称</th>
                        <th>面积</th>
                        <th>地址</th>
                        <th>类型</th>
                        <th>NodeId</th>
                      </tr>
                    </thead>
                    <tbody>
                      {item.tags.map((tag, rowIndex) => (
                        <tr key={rowIndex}>
                          <td>{tag.name}</td>
                          <td>{tag.area}</td>
                          <td className="mono">{tag.address}</td>
                          <td>{tag.data_type}</td>
                          <td className="mono">{tag.node_id || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}
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
