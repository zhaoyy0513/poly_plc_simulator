import type { SiteAccessDraft } from '../types'

interface Props {
  value: SiteAccessDraft
  onChange: (next: SiteAccessDraft) => void
  /** Shown instead of a placeholder when a password is already stored. */
  passwordPlaceholder?: string
}

/** How the platform reaches the machine this site represents. */
export function SiteAccessFields({ value, onChange, passwordPlaceholder }: Props) {
  const patch = (next: Partial<SiteAccessDraft>) => onChange({ ...value, ...next })
  const remote = value.host.trim().length > 0

  return (
    <>
      <div className="row">
        <label className="field grow">
          <span>目标机器 IP / 域名</span>
          <input
            className="input mono"
            value={value.host}
            placeholder="留空 = 本机，不经过 SSH"
            onChange={(event) => patch({ host: event.target.value })}
          />
        </label>
        <label className="field port-field">
          <span>SSH 端口</span>
          <input
            className="input mono"
            type="number"
            min={1}
            max={65535}
            value={value.ssh_port}
            onChange={(event) => patch({ ssh_port: Number(event.target.value) || 22 })}
          />
        </label>
      </div>

      {remote ? (
        <>
          <div className="row">
            <label className="field grow">
              <span>SSH 用户名</span>
              <input
                className="input mono"
                value={value.ssh_user}
                placeholder="例如 root"
                onChange={(event) => patch({ ssh_user: event.target.value })}
              />
            </label>
            <label className="field grow">
              <span>SSH 密码</span>
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                value={value.ssh_password}
                placeholder={passwordPlaceholder ?? '登录用的密码'}
                onChange={(event) => patch({ ssh_password: event.target.value })}
              />
            </label>
          </div>
          <label className="field">
            <span>部署目录（目标机上）</span>
            <input
              className="input mono"
              value={value.deploy_dir}
              placeholder="~/.poly-sim"
              onChange={(event) => patch({ deploy_dir: event.target.value })}
            />
          </label>
          <p className="hint">
            密码只用于 SSH 登录：保存后再打开不会显示，数据库里也是密文。留空则沿用已保存的密码。
          </p>
        </>
      ) : (
        <p className="hint">
          留空时这个 site 就是平台所在的这台机器，实例直接在本机起进程（现在的行为），不需要 SSH。
        </p>
      )}
    </>
  )
}
