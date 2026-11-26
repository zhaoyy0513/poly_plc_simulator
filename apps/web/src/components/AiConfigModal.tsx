import { useEffect, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store'
import type { AiConfig } from '../types'
import { Icon } from './Icon'
import { Modal } from './Modal'

interface Props {
  open: boolean
  onClose: () => void
}

export function AiConfigModal({ open, onClose }: Props) {
  const notify = useApp((state) => state.notify)
  const [config, setConfig] = useState<AiConfig | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [timeoutMs, setTimeoutMs] = useState('60000')
  const [enabled, setEnabled] = useState(false)
  const [models, setModels] = useState<string[]>([])
  const [tested, setTested] = useState(false)
  const [testing, setTesting] = useState(false)
  const [fetching, setFetching] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setError(null)
    setWarning(null)
    api
      .aiConfig()
      .then((cfg) => {
        setConfig(cfg)
        setBaseUrl(cfg.base_url)
        setModel(cfg.model)
        setTimeoutMs(String(cfg.timeout_ms))
        setEnabled(cfg.enabled)
        setApiKey('')
        if (cfg.configured) {
          setTested(true)
          // 已配置过的，打开就把模型列表悄悄补上；失败不打扰用户。
          api.aiModels().then((res) => setModels(res.models)).catch(() => {})
        } else {
          setTested(false)
          setModels([])
        }
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [open])

  const runTest = async () => {
    const url = baseUrl.trim()
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      setError('Base URL 必须以 http:// 或 https:// 开头')
      return
    }
    if (!apiKey.trim() && !config?.configured) {
      setError('请先填 API Key')
      return
    }
    setTesting(true)
    setError(null)
    setWarning(null)
    try {
      const result = await api.testAiConfig({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || undefined,
        timeout_ms: Number(timeoutMs),
      })
      // 无论模型列表是否拉到，都允许手动输入模型继续配置。
      setTested(true)
      if (!result.ok) {
        setError(result.detail || '连接失败')
        return
      }
      setModels(result.models)
      setWarning(result.detail || null)
      if (!model || !result.models.includes(model)) {
        setModel(result.models[0] ?? '')
      }
      notify('ok', result.models.length ? `连接正常，拉到 ${result.models.length} 个模型` : '连接正常')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setTesting(false)
    }
  }

  const fetchModels = async () => {
    setFetching(true)
    setError(null)
    setWarning(null)
    try {
      const result = await api.testAiConfig({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || undefined,
        timeout_ms: Number(timeoutMs),
      })
      if (!result.ok) {
        setError(result.detail || '获取模型失败')
        return
      }
      setModels(result.models)
      setTested(true)
      setWarning(result.detail || null)
      if (!model || !result.models.includes(model)) {
        setModel(result.models[0] ?? '')
      }
      notify('ok', result.models.length ? `拉到 ${result.models.length} 个模型` : '连接正常，但未返回模型列表')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setFetching(false)
    }
  }

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

  const modelDisabled = !tested

  return (
    <Modal
      open={open}
      title="AI 配置"
      subtitle="OpenAI 兼容端点；密钥加密存储，页面只显示是否已配置。"
      width={640}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={busy || !baseUrl.trim() || !model.trim()}
            onClick={() => void save()}
          >
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
          onChange={(event) => {
            setBaseUrl(event.target.value)
            setTested(false)
          }}
        />
      </label>

      <div className="row">
        <label className="field grow">
          <span>API Key</span>
          <input
            className="input mono"
            type="password"
            value={apiKey}
            placeholder={config?.configured ? '留空表示保留原密钥' : '输入 API Key'}
            onChange={(event) => {
              setApiKey(event.target.value)
              setTested(false)
            }}
          />
        </label>
        <button
          type="button"
          className="btn ghost"
          disabled={testing || fetching || !baseUrl.trim()}
          onClick={() => void runTest()}
        >
          <Icon name="plug" size={15} className={testing ? 'spin' : undefined} />
          {testing ? '测试中…' : '测试连接'}
        </button>
      </div>

      <div className="row">
        <label className="field grow">
          <span>模型</span>
          <div className="model-row">
            <input
              className="input mono"
              list="ai-model-list"
              value={model}
              disabled={modelDisabled}
              placeholder={tested ? '选择或输入模型' : '先测试连接'}
              onChange={(event) => setModel(event.target.value)}
            />
            <button
              type="button"
              className="btn ghost"
              disabled={fetching || testing || !baseUrl.trim() || modelDisabled}
              onClick={() => void fetchModels()}
            >
              <Icon name="restart" size={15} className={fetching ? 'spin' : undefined} />
              {fetching ? '获取中…' : '获取模型'}
            </button>
          </div>
          <datalist id="ai-model-list">
            {models.map((item) => (
              <option key={item} value={item} />
            ))}
          </datalist>
          {models.length ? (
            <div className={`model-chips${modelDisabled ? ' disabled' : ''}`}>
              {models.map((item) => (
                <button
                  type="button"
                  key={item}
                  className={`chip${model === item ? ' active' : ''}`}
                  disabled={modelDisabled}
                  onClick={() => setModel(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          ) : null}
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

      <div className="ai-enable-row">
        <label className="switch">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span className="slider" />
        </label>
        <div className="ai-enable-copy">
          <strong>启用 AI 点表分析</strong>
          <small>关闭后分析入口不可用；已创建的实例不受影响。</small>
        </div>
      </div>

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}

      {warning ? (
        <div className="notice">
          <Icon name="alert" size={15} /> {warning}
        </div>
      ) : null}
    </Modal>
  )
}
