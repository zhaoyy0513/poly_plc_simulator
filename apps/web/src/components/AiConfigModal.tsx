import { useEffect, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store'
import { Icon } from './Icon'
import { Modal } from './Modal'

interface Props {
  open: boolean
  onClose: () => void
}

export function AiConfigModal({ open, onClose }: Props) {
  const notify = useApp((state) => state.notify)
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [timeoutMs, setTimeoutMs] = useState('60000')
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setError(null)
    api
      .aiConfig()
      .then((cfg) => {
        setBaseUrl(cfg.base_url)
        setModel(cfg.model)
        setTimeoutMs(String(cfg.timeout_ms))
        setEnabled(cfg.enabled)
        // Never prefill the key: the API does not return it.
        setApiKey('')
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [open])

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.saveAiConfig({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || undefined,
        model: model.trim(),
        enabled,
        timeout_ms: Number(timeoutMs),
      })
      notify('ok', 'AI 配置已保存')
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const test = async () => {
    setTesting(true)
    setError(null)
    try {
      const result = await api.testAiConfig()
      if (result.ok) notify('ok', 'AI 连接正常')
      else setError(result.detail || '连接失败')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setTesting(false)
    }
  }

  return (
    <Modal
      open={open}
      title="AI 配置"
      subtitle="OpenAI 兼容端点；密钥加密存储，页面只显示是否已配置。"
      width={560}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn primary" disabled={busy || !baseUrl.trim() || !model.trim()} onClick={() => void save()}>
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <label className="field">
        <span>Base URL</span>
        <input
          className="input mono"
          value={baseUrl}
          placeholder="https://api.openai.com/v1"
          onChange={(event) => setBaseUrl(event.target.value)}
        />
      </label>
      <label className="field">
        <span>API Key</span>
        <input
          className="input mono"
          type="password"
          value={apiKey}
          placeholder="留空表示保留原密钥"
          onChange={(event) => setApiKey(event.target.value)}
        />
      </label>
      <div className="row">
        <label className="field grow">
          <span>模型</span>
          <input
            className="input mono"
            value={model}
            placeholder="gpt-4o-mini"
            onChange={(event) => setModel(event.target.value)}
          />
        </label>
        <label className="field port-field">
          <span>超时（毫秒）</span>
          <input
            className="input mono"
            type="number"
            min={5000}
            max={300000}
            value={timeoutMs}
            onChange={(event) => setTimeoutMs(event.target.value)}
          />
        </label>
      </div>
      <label className="field checkbox-field">
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        <span>启用 AI 点表分析</span>
      </label>
      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}
      <div className="modal-actions">
        <button type="button" className="btn ghost" disabled={testing || !baseUrl.trim() || !model.trim()} onClick={() => void test()}>
          <Icon name="plug" size={15} /> {testing ? '测试中…' : '测试连接'}
        </button>
      </div>
    </Modal>
  )
}
