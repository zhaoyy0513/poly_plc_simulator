import { altRefText, areaMeta, parseStart, refText } from '../src/modbusAddress.ts'

const cases: [string, number | null, string][] = [
  ['holding', 0, '0'],
  ['holding', 0, '40001'],
  ['holding', 0, '400001'],
  ['holding', 99, '40100'],
  ['holding', 99, '400100'],
  ['holding', 9998, '49999'],
  ['holding', 9999, '410000'],
  ['holding', 65535, '465536'],
  ['holding', 65535, '65535'],
  ['holding', 40000, '40000'],
  ['holding', null, '70000'],
  ['holding', null, 'abc'],
  ['input', 0, '30001'],
  ['input', 0, '300001'],
  ['discrete', 0, '10001'],
  ['discrete', 0, '100001'],
  ['coil', 0, '00001'],
  ['coil', 0, '000001'],
  ['coil', 1, '1'],
]
// 偏移 -> 显示的引用号
const refs: [string, number, string][] = [
  ['holding', 0, '40001'],
  ['holding', 9998, '49999'],
  ['holding', 9999, '410000'],
  ['holding', 65535, '465536'],
  ['input', 0, '30001'],
  ['discrete', 0, '10001'],
  ['coil', 0, '00001'],
  ['coil', 9998, '09999'],
  ['coil', 9999, '010000'],
]
const alts: [string, number, string | null][] = [
  ['holding', 0, '400001'],
  ['coil', 0, '000001'],
  ['holding', 9999, null],
]

let bad = 0
for (const [area, want, text] of cases) {
  const got = parseStart(text, area as never)
  if (got !== want) {
    bad++
    console.log(`FAIL parseStart(${area}, "${text}") = ${got}, want ${want}`)
  }
}
for (const [area, offset, want] of refs) {
  const got = refText(areaMeta(area as never), offset)
  if (got !== want) {
    bad++
    console.log(`FAIL refText(${area}, ${offset}) = ${got}, want ${want}`)
  }
}
for (const [area, offset, want] of alts) {
  const got = altRefText(areaMeta(area as never), offset)
  if (got !== want) {
    bad++
    console.log(`FAIL altRefText(${area}, ${offset}) = ${got}, want ${want}`)
  }
}
console.log(bad === 0 ? `全部通过（${cases.length + refs.length + alts.length} 条）` : `${bad} 条失败`)
