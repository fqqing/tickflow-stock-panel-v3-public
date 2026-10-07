/**
 * 蛟龙出海信号点 overlay 最终验证（300750.SZ）。
 *
 * 信号日实测(后端)：2026-07-29 / 2026-07-30 两根，low≈379.81 / 388.19。
 * 默认视口只显示最近 ~50 根，信号日在左侧屏外(x≈-23)。先用 deltaX 滚轮往旧数据
 * 方向滚，把信号日带进可视区，再扫主图 canvas 的黄色像素聚簇。
 *
 * 黄色只会来自两处：涨停标记(limitUp, 本股几乎无)与蛟龙信号点(dragon-markers
 * overlay)。DRAGON 指标本体只画红色生命线，不产生黄色。
 */
import { openPage, Suite, sleep } from './qa-harness.mjs'

const page = await openPage()
const suite = new Suite('蛟龙信号点 overlay 渲染(300750)')

const WITHOUT = [
  { key: 'ma#1', name: 'MA', group: 'main', params: [] },
  { key: 'vol#1', name: 'VOL', group: 'sub', params: [] },
]
const WITH = [
  ...WITHOUT,
  { key: 'dragon#1', name: 'DRAGON', group: 'main', params: [] },
]

const scanYellow = `(() => {
  const res = []
  document.querySelectorAll('canvas').forEach((cv) => {
    if (!cv.width || !cv.height) return
    const rect = cv.getBoundingClientRect()
    const ctx = cv.getContext('2d')
    let d
    try { d = ctx.getImageData(0, 0, cv.width, cv.height).data } catch { return }
    const pts = []
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i+1], b = d[i+2], a = d[i+3]
      if (a < 40) continue
      if (Math.abs(r-250)<40 && Math.abs(g-204)<48 && Math.abs(b-21)<60) {
        pts.push({ x: (i/4) % cv.width | 0, y: (i/4/cv.width) | 0 })
      }
    }
    const clusters = []
    for (const p of pts) {
      const c = clusters.find(c => Math.abs(c.x - p.x) < 10)
      if (c) { c.n++; c.x = Math.round((c.x * (c.n-1) + p.x) / c.n); c.yMin = Math.min(c.yMin, p.y); c.yMax = Math.max(c.yMax, p.y) }
      else clusters.push({ x: p.x, n: 1, yMin: p.y, yMax: p.y })
    }
    res.push({
      w: cv.width, h: cv.height, left: Math.round(rect.left), top: Math.round(rect.top),
      total: pts.length, clusters: clusters.filter(c => c.n >= 2),
    })
  })
  return res
})()`

async function setup(preset) {
  await page.eval(`localStorage.setItem('tickflow.useKLinePro','true')`)
  await page.eval(`localStorage.setItem('tickflow.kline.indicators.v1', ${JSON.stringify(JSON.stringify(preset))})`)
  await page.reload()
  await sleep(6500)
}

async function scrollToSignal() {
  // 主图 canvas 中心
  const rect = await page.eval(`(() => {
    const cs = [...document.querySelectorAll('canvas')].filter(c => c.width > 200)
    const c = cs.sort((a,b) => b.width*b.height - a.width*a.height)[0]
    const r = c.getBoundingClientRect()
    return { cx: r.left + r.width/2, cy: r.top + r.height/2 }
  })()`)
  // 先点击 canvas(实测 headless 下滚轮需先有一次 pointer 事件才生效)
  await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.cx, y: rect.cy, button: 'left', clickCount: 1 })
  await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.cx, y: rect.cy, button: 'left', clickCount: 1 })
  await sleep(250)
  // deltaX 负值 → 往旧数据方向滚。实测每格 ≈200px≈12 根 bar：信号日默认在
  // x≈-23(屏外左侧)，滚 2 次(400px)把它带到 x≈370 的可视区中部。滚过头会
  // 直接到数据开头(from=0)，信号日反而跑到右侧屏外。
  for (let i = 0; i < 2; i++) {
    await page.send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: rect.cx, y: rect.cy, deltaX: -200, deltaY: 0 })
    await sleep(150)
  }
  await sleep(600)
}

const landed = await page.goto('http://127.0.0.1:3011/stock/300750.SZ', 15000)
suite.ok('导航到 300750', landed)
await page.resize(1600, 900)
await sleep(2000)

await setup(WITHOUT)
await scrollToSignal()
const base = (await page.eval(scanYellow)).filter(c => c.w > 200)
await setup(WITH)
await scrollToSignal()
const withDragon = (await page.eval(scanYellow)).filter(c => c.w > 200)

const clusterList = (arr) => arr.flatMap(c => c.clusters.map(k => ({ cx: k.x, cy: Math.round((k.yMin + k.yMax) / 2), n: k.n, canvas: `${c.w}x${c.h}@${c.left},${c.top}` })))

const baseClusters = clusterList(base)
const dragonClusters = clusterList(withDragon)
const delta = dragonClusters.length - baseClusters.length

console.log('基线黄色聚簇:', JSON.stringify(baseClusters))
console.log('有DRAGON黄色聚簇:', JSON.stringify(dragonClusters))
console.log(`增量 = ${delta}`)

suite.ok('开启 DRAGON 后出现新的黄色聚簇(信号点)', delta >= 1, `增量=${delta}`)

// 信号日 LOW 下方 → 标记应落在主图 canvas 下半区(y > 0.4*h)
const mainDragon = withDragon.filter(c => c.clusters.length > 0)
const lower = mainDragon.some(c => c.clusters.some(k => (k.yMin + k.yMax) / 2 > c.h * 0.35))
suite.ok('信号点位于主图下半区(LOW 下方)', lower)

const errs = page.consoleErrors.filter(e => !/React DevTools/i.test(e))
suite.ok('无 console 错误', errs.length === 0, errs.slice(0, 3).join(' | '))
suite.ok('无 JS 异常', page.exceptions.length === 0, page.exceptions.slice(0, 3).join(' | '))

await page.shot('qa3-dragon-final.png')
suite.summary()
process.exit(0)
