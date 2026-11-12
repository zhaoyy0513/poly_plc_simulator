import { useEffect, useState } from 'react'
import {
  DATA_TYPES,
  MODBUS_AREAS,
  S7_AREAS,
  SIM_MODES,
  defaultTag,
  isLink,
  type DataType,
  type Server,
  type SimMode,
  type Tag,
} from '../types'
import { useConfirm } from './ConfirmModal'
import { Icon } from './Icon'

interface Props {
  server: Server
  onSave: (tags: Tag[]) => Promise<boolean>
  saving: boolean
}

const clone = (tags: Tag[]): Tag[] => tags.map((tag) => ({ ...tag, sim: { ...tag.sim } }))

export function TagsEditor({ server, onSave, saving }: Props) {
  const [draft, setDraft] = useState<Tag[]>(() => clone(server.tags))
  const [dirty, setDirty] = useState(false)
  const confirm = useConfirm()

  useEffect(() => {
    setDraft(clone(server.tags))
    setDirty(false)
  }, [server.id, server.config_version])

  const update = (index: number, patch: Partial<Tag>) => {
    setDraft((current) => current.map((tag, position) => (position === index ? { ...tag, ...patch } : tag)))
    setDirty(true)
  }

  const updateSim = (index: number, patch: Partial<Tag['sim']>) => {
    setDraft((current) =>
      current.map((tag, position) =>
        position === index ? { ...tag, sim: { ...tag.sim, ...patch } } : tag,
      ),
    )
    setDirty(true)
  }

  const changeType = (index: number, dataType: DataType) => {
    const patch: Partial<Tag> = { data_type: dataType }
    if (dataType === 'bool') {
      if (server.protocol === 's7') patch.bit = draft[index].bit ?? 0
    } else {
      patch.bit = null
    }
    update(index, patch)
  }

  const changeArea = (index: number, area: string) => {
    const patch: Partial<Tag> = { area }
    if (server.protocol === 's7' && area === 'db') patch.db_number = draft[index].db_number ?? 1
    if (server.protocol === 's7' && area !== 'db') patch.db_number = null
    if (server.protocol === 'modbus_tcp' && (area === 'coil' || area === 'discrete')) {
      patch.data_type = 'bool'
      patch.bit = null
    }
    update(index, patch)
  }

  const add = () => {
    const tag = defaultTag(server.protocol)
    tag.name = `tag_${draft.length + 1}`
    setDraft((current) => [...current, tag])
    setDirty(true)
  }

  const remove = (index: number) => {
    setDraft((current) => current.filter((_, position) => position !== index))
    setDirty(true)
  }

  const clear = async () => {
    if (!draft.length) return
    const ok = await confirm({
      title: '清空标签',
      message: '清空全部标签？保存后实例会重启，未定义地址的值归零。',
      confirmText: '清空',
      danger: true,
    })
    if (!ok) return
    setDraft([])
    setDirty(true)
  }

  const save = async () => {
    const ok = await onSave(draft)
    if (ok) setDirty(false)
  }

  const isModbus = server.protocol === 'modbus_tcp'
  const isS7 = server.protocol === 's7'
  const isOpcUa = server.protocol === 'opcua'
  // A sim owns its address space, so the variable name *is* the NodeId. A link
  // has to be told the far side's NodeId, because that server names things its
  // own way - and a typo there is the likeliest mistake on this protocol.
  const showNodeId = isOpcUa && isLink(server)
  const namespaceIndex = server.target_options?.namespace_index ?? 2

  return (
    <div className="tags-editor">
      <div className="tags-toolbar">
        <div>
          <strong>标签表</strong>
          <p className="hint">
            {isModbus
              ? '地址为 0 基偏移：HR/IR 按寄存器，CO/DI 按位。32/64 位值可切换字序。'
              : isS7
                ? '地址为字节偏移：DB 需填 DB 号，bool 需指定位号；S7 固定大端。'
                : showNodeId
                  ? `NodeId 留空即按「ns=${namespaceIndex};s=变量名」推导。连第三方 server 时填它的真实 NodeId，形如 ns=4;s=Main.Temp。`
                  : '变量名即 NodeId（字符串），会挂到 Objects/<实例名>/ 下。'}
          </p>
        </div>
        <div className="tags-actions">
          <button type="button" className="btn ghost" onClick={add}>
            <Icon name="plus" size={15} /> 添加变量
          </button>
          {draft.length ? (
            <button type="button" className="btn ghost" onClick={() => void clear()}>
              清空
            </button>
          ) : null}
          <button type="button" className="btn primary" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? '保存中…' : '保存并重启'}
          </button>
        </div>
      </div>

      <div className="table-wrap">
        <table className="data-table tags-table">
          <thead>
            <tr>
              <th>变量名</th>
              <th>类型</th>
              {showNodeId ? <th>NodeId</th> : null}
              {!isOpcUa ? <th>区域</th> : null}
              {!isOpcUa ? <th>地址</th> : null}
              {isS7 ? <th>位 / DB</th> : null}
              {!isS7 && !isOpcUa ? <th>字序</th> : null}
              <th>写</th>
              <th>仿真</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.map((tag, index) => (
              <tr key={`${tag.id ?? 'new'}-${index}`}>
                <td>
                  <input
                    className="input mono compact"
                    value={tag.name}
                    onChange={(event) => update(index, { name: event.target.value })}
                  />
                  <input
                    className="input compact desc-input"
                    value={tag.description}
                    placeholder="描述"
                    onChange={(event) => update(index, { description: event.target.value })}
                  />
                </td>
                <td>
                  <select
                    className="input compact"
                    value={tag.data_type}
                    onChange={(event) => changeType(index, event.target.value as DataType)}
                  >
                    {DATA_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </select>
                </td>
                {showNodeId ? (
                  <td>
                    <input
                      className="input mono compact"
                      value={tag.node_id ?? ''}
                      placeholder={`ns=${namespaceIndex};s=${tag.name}`}
                      title="留空则按「命名空间索引 + 变量名」推导"
                      onChange={(event) =>
                        update(index, { node_id: event.target.value || null })
                      }
                    />
                  </td>
                ) : null}
                {!isOpcUa ? (
                  <td>
                    <select
                      className="input compact"
                      value={tag.area}
                      onChange={(event) => changeArea(index, event.target.value)}
                    >
                      {(isModbus ? MODBUS_AREAS : S7_AREAS).map((area) => (
                        <option key={area} value={area}>
                          {area}
                        </option>
                      ))}
                    </select>
                  </td>
                ) : null}
                {!isOpcUa ? (
                  <td>
                    <input
                      className="input compact num"
                      type="number"
                      min={0}
                      value={tag.address}
                      onChange={(event) => update(index, { address: Number(event.target.value) })}
                    />
                  </td>
                ) : null}
                {isS7 ? (
                  <td>
                    <div className="stack">
                      {tag.data_type === 'bool' ? (
                        <input
                          className="input compact num"
                          type="number"
                          min={0}
                          max={7}
                          value={tag.bit ?? 0}
                          title="bit"
                          onChange={(event) => update(index, { bit: Number(event.target.value) })}
                        />
                      ) : null}
                      {tag.area === 'db' ? (
                        <input
                          className="input compact num"
                          type="number"
                          min={1}
                          value={tag.db_number ?? 1}
                          title="DB 号"
                          onChange={(event) => update(index, { db_number: Number(event.target.value) })}
                        />
                      ) : null}
                    </div>
                  </td>
                ) : null}
                {!isS7 && !isOpcUa ? (
                  <td>
                    {['int32', 'uint32', 'float32', 'float64'].includes(tag.data_type) ? (
                      <select
                        className="input compact"
                        value={tag.word_order}
                        onChange={(event) =>
                          update(index, { word_order: event.target.value as Tag['word_order'] })
                        }
                      >
                        <option value="big">AB-CD</option>
                        <option value="little">CD-AB</option>
                      </select>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                ) : null}
                <td>
                  <input
                    type="checkbox"
                    className="check"
                    checked={tag.writable}
                    onChange={(event) => update(index, { writable: event.target.checked })}
                    aria-label="可写"
                  />
                </td>
                <td>
                  <div className="stack">
                    <select
                      className="input compact"
                      value={tag.sim.mode}
                      onChange={(event) => updateSim(index, { mode: event.target.value as SimMode })}
                    >
                      {SIM_MODES.map((mode) => (
                        <option key={mode} value={mode}>
                          {mode}
                        </option>
                      ))}
                    </select>
                    {tag.sim.mode !== 'static' ? (
                      <div className="sim-params">
                        {tag.sim.mode !== 'toggle' ? (
                          <>
                            <input
                              className="input compact num"
                              type="number"
                              value={tag.sim.min}
                              title="min"
                              onChange={(event) => updateSim(index, { min: Number(event.target.value) })}
                            />
                            <input
                              className="input compact num"
                              type="number"
                              value={tag.sim.max}
                              title="max"
                              onChange={(event) => updateSim(index, { max: Number(event.target.value) })}
                            />
                          </>
                        ) : null}
                        {tag.sim.mode === 'sine' || tag.sim.mode === 'toggle' ? (
                          <input
                            className="input compact num"
                            type="number"
                            min={200}
                            value={tag.sim.period_ms}
                            title="period ms"
                            onChange={(event) =>
                              updateSim(index, { period_ms: Number(event.target.value) })
                            }
                          />
                        ) : null}
                        {tag.sim.mode === 'ramp' ? (
                          <input
                            className="input compact num"
                            type="number"
                            value={tag.sim.step}
                            title="step"
                            onChange={(event) => updateSim(index, { step: Number(event.target.value) })}
                          />
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </td>
                <td>
                  <button type="button" className="icon-btn danger" title="删除变量" onClick={() => remove(index)}>
                    <Icon name="trash" size={15} />
                  </button>
                </td>
              </tr>
            ))}
            {draft.length === 0 ? (
              <tr>
                <td colSpan={showNodeId ? 10 : 9} className="table-empty">
                  暂无变量，点「添加变量」开始。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <p className="hint">
        保存会重建实例的数据面（寄存器 / 字节区 / 地址空间），已连接的客户端需要重连。变量名重复、地址重叠会被后端拒绝。
      </p>
    </div>
  )
}
