/**
 * Modbus reference numbers, in one place.
 *
 * Modbus itself only knows 0-based offsets (0–65535). Humans read device
 * manuals that number the same registers per area: 40001 is the first holding
 * register, 30001 the first input register, 10001 the first discrete input and
 * 00001 the first coil. Two widths are in use and both mean the same address:
 *
 *   40001   (5 digits) = holding offset 0      — the classic Modicon block,
 *   400001  (6 digits) = holding offset 0        the extended form used once a
 *                                                map grows past 9999 points.
 *
 * Getting this wrong is a silent off-by-a-lot: typing 40001 used to be read as
 * "offset 40001", so the page showed a different register than the manual.
 */
import type { ModbusArea } from './types'

export interface AreaMeta {
  value: ModbusArea
  fc: string
  label: string
  cn: string
  bits: boolean
  /** Prefix of the extended (6-digit) reference block, e.g. 400000. */
  refBase: number
}

export const AREAS: AreaMeta[] = [
  { value: 'coil', fc: '01', label: 'Coils', cn: '线圈', bits: true, refBase: 0 },
  { value: 'discrete', fc: '02', label: 'Discrete Inputs', cn: '离散输入', bits: true, refBase: 100000 },
  { value: 'holding', fc: '03', label: 'Holding Registers', cn: '保持寄存器', bits: false, refBase: 400000 },
  { value: 'input', fc: '04', label: 'Input Registers', cn: '输入寄存器', bits: false, refBase: 300000 },
]

/** Points the classic 5-digit block can address, i.e. offsets 0–9998. */
const NARROW_MAX = 9998

export function areaMeta(area: ModbusArea): AreaMeta {
  return AREAS.find((item) => item.value === area) ?? AREAS[2]
}

/**
 * Start-address text to a 0-based offset. Accepts the reference number in
 * either width, and a bare number means an offset.
 */
export function parseStart(text: string, area: ModbusArea): number | null {
  const trimmed = text.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  if (!Number.isSafeInteger(value)) return null

  const asReference = (narrowBase: number, wideBase: number): number | null => {
    // 5 digits: classic block, e.g. 40001..49999.
    if (trimmed.length === 5 && value > narrowBase && value <= narrowBase + 9999) {
      return value - narrowBase - 1
    }
    // 6 digits: extended block, e.g. 400001..465536 for offsets 0..65535.
    if (trimmed.length === 6 && value > wideBase && value <= wideBase + 65536) {
      return value - wideBase - 1
    }
    return null
  }

  if (area === 'holding') {
    const reference = asReference(40000, 400000)
    if (reference !== null) return reference
  }
  if (area === 'input') {
    const reference = asReference(30000, 300000)
    if (reference !== null) return reference
  }
  if (area === 'discrete') {
    const reference = asReference(10000, 100000)
    if (reference !== null) return reference
  }
  // Coil refs are zero-padded (00001 / 000001), which is what makes them
  // distinguishable from a plain offset of 1.
  if (area === 'coil' && /^0\d{4,5}$/.test(trimmed) && value >= 1 && value <= 65536) {
    return value - 1
  }
  if (value < 0 || value > 65535) return null
  return value
}

/** Offset to the reference number, in the width a device manual would use. */
export function refText(meta: AreaMeta, offset: number): string {
  const narrow = offset <= NARROW_MAX
  if (meta.value === 'coil') return String(offset + 1).padStart(narrow ? 5 : 6, '0')
  const base = narrow ? meta.refBase / 10 : meta.refBase
  return String(base + offset + 1)
}

/**
 * The other width of the same address, for the "= 400001" hint. Null when the
 * classic 5-digit block cannot express the offset at all (it stops at 9998).
 */
export function altRefText(meta: AreaMeta, offset: number): string | null {
  if (offset > NARROW_MAX) return null
  if (meta.value === 'coil') return String(offset + 1).padStart(6, '0')
  return String(meta.refBase + offset + 1)
}
