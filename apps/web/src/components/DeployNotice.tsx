import { Icon } from './Icon'
import type { DeployPlan } from '../types'

interface Props {
  plan: DeployPlan | null
  host: string
  probing?: boolean
}

/**
 * "This is how your instances will run" - a statement, not a question.
 *
 * The platform infers the carrier from what the machine actually has. Users
 * who dislike the answer fix the machine (install Python, open the network)
 * and come back; there is deliberately no selector here.
 */
export function DeployNotice({ plan, host, probing = false }: Props) {
  if (probing) {
    return (
      <div className="carrier-box pending">
        <span className="carrier-icon">
          <Icon name="plug" size={14} />
        </span>
        <div className="carrier-body">
          <strong>正在探测 {host}…</strong>
          <p>确认 SSH、系统版本、Python 和端口占用，再决定用哪种方式部署。</p>
        </div>
      </div>
    )
  }

  if (!plan || !plan.carrier) return null

  const tone = plan.carrier === 'unknown' ? 'pending' : plan.ready ? '' : 'bad'
  return (
    <div className={`carrier-box${tone ? ` ${tone}` : ''}`}>
      <span className="carrier-icon">
        <Icon name={plan.carrier === 'unknown' ? 'plug' : 'layers'} size={14} />
      </span>
      <div className="carrier-body">
        <strong>部署方式：{plan.label}</strong>
        <p>{plan.reason}</p>
        {plan.action ? <span className="carrier-steps">{plan.action}</span> : null}
      </div>
    </div>
  )
}
