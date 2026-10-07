/**
 * 像素级验证：蛟龙圆点(黄 #FACC15 主图) + 结构标注文字(橙 #F59E0B MACD副图)。
 */
import { openPage, Suite, sleep } from './qa-harness.mjs'

const page = await openPage()
const suite = new Suite('像素级验证(300750)')

const preset = [
  { key: 'ma#1', name: 'MA', group: 'main', params: [] },
  { key: 'vol#1', name: 'VOL', group: 'sub', params: [] },
  { key: 'momentum#1', name: 'MOMENTUM', group: 'sub', params: [] },
  { key: 'macd_quant#1', name: 'MACD_QUANT', group: 'sub', params: [] },
  { key: 'dragon#1', name: 'DRAGON', group: 'main', params: [] },
]

const landed = await page.goto('http://127.0.0.1:3011/stock/300750.SZ', 15000)
suite.ok('导航到 300750', landed)
await page.resize(1600, 900)
await sleep(2500)
await page.eval(`localStorage.setItem('tickflow.kline.indicators.v1', ${JSON.stringify(JSON.stringify(preset))})`)
await page.reload()
await sleep(6500)

const scan = await page.eval(`(() => {
  const near = (r,g,b,R,G,B,tol=28) => Math.abs(r-R)<tol && Math.abs(g-G)<tol && Math.abs(b-B)<tol
  const summary = {}
  document.querySelectorAll('canvas').forEach((cv, ci) => {
    if (!cv.width || !cv.height) return
    let ctx
    try { ctx = cv.getContext('2d') } catch { return }
    if (!ctx) return
    const rect = cv.getBoundingClientRect()
    let d
    try { d = ctx.getImageData(0, 0, cv.width, cv.height).data } catch { return }
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i+1], b = d[i+2]
      let name = null
      if (near(r,g,b, 245,158,11)) name = 'orange-structure'
      else if (near(r,g,b, 250,204,21)) name = 'yellow-dragon'
      if (!name) continue
      const gy = Math.round(rect.top) + ((i / 4 / cv.width) | 0)
      const s = summary[name] = summary[name] || { count: 0, yMin: gy, yMax: gy, canvases: new Set() }
      s.count += 1
      s.yMin = Math.min(s.yMin, gy)
      s.yMax = Math.max(s.yMax, gy)
      s.canvases.add(ci)
    }
  })
  for (const s of Object.values(summary)) s.canvases = [...s.canvases]
  return summary
})()`)
console.log('像素分布:', JSON.stringify(scan, null, 1))

const H = 760
const s = scan ?? {}
// 主图区(y < 55% 视口)的黄色 = 蛟龙信号圆点/距9连阳文字(阈值线在资金动能副图, 更靠下)
const dragonY = s['yellow-dragon']?.yMin ?? -1
suite.ok('蛟龙黄色像素存在', (s['yellow-dragon']?.count ?? 0) > 0, JSON.stringify(s['yellow-dragon']))
suite.ok('蛟龙黄色在主图区(上半)', dragonY >= 0 && dragonY < H * 0.55, `yMin=${dragonY}`)
// 橙色 = 底/顶结构标注文字(MACD 副图区, y > 55%)
suite.ok('结构标注橙色像素存在', (s['orange-structure']?.count ?? 0) > 0, JSON.stringify(s['orange-structure']))

await page.shot('qa3-300750-pixel.png')

const errs = page.consoleErrors.filter(e => !/React DevTools/i.test(e))
suite.ok('无 console 错误', errs.length === 0, errs.slice(0, 3).join(' | '))
suite.summary()
process.exit(0)
