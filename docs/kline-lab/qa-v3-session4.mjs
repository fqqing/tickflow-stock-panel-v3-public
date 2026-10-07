/** 第四轮：视口健康后复测 加载速度/指标面板/标记像素/复盘页 */
import { openPage, Suite, sleep } from './qa-harness.mjs'
const BASE = 'http://127.0.0.1:3011'
const OUT = new URL('.', import.meta.url).pathname.replace(/^\/([A-Za-z]):/, '$1:')
const s = new Suite('复测')
const page = await openPage()
const shot = async f => {
  const r = await page.send('Page.captureScreenshot', { format: 'png' })
  if (r.result?.data) (await import('node:fs')).writeFileSync(f, Buffer.from(r.result.data, 'base64'))
}
const gotoResize = async u => { const ok = await page.goto(u, 15000); await page.resize(1600, 900); return ok }
const clickByText = t => page.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='${t}');if(!b)return false;b.click();return true})()`)
const waitInk = async (t = 20000) => {
  const t0 = Date.now()
  while (Date.now() - t0 < t) {
    const ink = await page.eval(`(()=>{let ink=0,total=0;for(const c of document.querySelectorAll('canvas')){if(!c.width||c.height<150)continue;const ctx=c.getContext('2d');if(!ctx)continue;let d;try{d=ctx.getImageData(0,0,c.width,c.height).data}catch(e){return -2}for(let i=3;i<d.length;i+=4*53){total++;if(d[i]>8)ink++}}return total?+(ink/total).toFixed(4):-1})()`).catch(() => -9)
    if (ink >= 0.02) return Date.now() - t0
    await sleep(200)
  }
  return -1
}

// 布局
let t0 = Date.now()
await gotoResize(`${BASE}/stock/600522.SH`)
const vw = await page.eval('[window.innerWidth, window.innerHeight, document.body.getBoundingClientRect().height]')
s.ok('视口 1600x900 + body 满高', vw[0] === 1600 && vw[1] === 900 && vw[2] > 500, JSON.stringify(vw))
const ink1 = await waitInk()
s.ok('首次加载 K 线渲染', ink1 > 0, `${ink1}ms (navigate→ink)`)
t0 = Date.now(); await page.reload(); await page.resize(1600, 900)
const ink2 = await waitInk()
s.ok('reload 重渲染', ink2 > 0, `${ink2}ms`)

// 切周期
let a = await (async () => { const t = Date.now(); await clickByText('周'); const m = await waitInk(15000); return m })()
let b = await (async () => { const t = Date.now(); await clickByText('日'); const m = await waitInk(15000); return m })()
s.ok('切周期 周/日', a > 0 && b > 0, `周 ${a}ms / 日 ${b}ms`)
const kt = await page.eval(`performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/kline')).map(r=>Math.round(r.duration)).slice(-6)`)
s.note('kline 接口耗时(ms)', JSON.stringify(kt))

// 信号标记像素（信号在上轮已开, localStorage 无状态; 再点一次确保开）
await clickByText('信号').catch(() => {})
await sleep(3000)
const px = await page.eval(`(()=>{let red=0,green=0,blue=0;for(const c of document.querySelectorAll('canvas')){if(!c.width||c.height<150)continue;const ctx=c.getContext('2d');if(!ctx)continue;let d;try{d=ctx.getImageData(0,0,c.width,c.height).data}catch(e){continue}for(let i=0;i<d.length;i+=4){const r=d[i],g=d[i+1],bb=d[i+2],al=d[i+3];if(al<40)continue;if(r>140&&r>g+50&&r>bb+50)red++;else if(g>130&&g>r+40&&g>bb+30)green++;else if(bb>150&&bb>r+40&&bb>g+20)blue++}}return{red,green,blue}})()`)
s.note('标记像素 red/green/blue', JSON.stringify(px))
s.ok('图表有红绿墨(蜡烛本身)', px.red + px.green > 500, JSON.stringify(px))
await shot(OUT + 'qa3d-terminal-sig.png')

// 指标面板
const indOpened = await clickByText('指标 2')
await sleep(900)
const panel = await page.eval('document.body.innerText')
s.ok('指标面板打开', indOpened && panel.includes('主图') && panel.includes('副图'))
s.ok('指标面板含 MACD', panel.includes('MACD'))
s.note('面板含 资金动能/蛟龙出海/MACD定量结构', JSON.stringify([panel.includes('资金动能'), panel.includes('蛟龙出海'), panel.includes('MACD定量结构')]))
await shot(OUT + 'qa3d-indicator-panel.png')
await clickByText('指标 2').catch(() => {})
await sleep(300)

// 定量结构开关像素 A/B
const d0 = await page.canvasDigest()
await clickByText('定量结构')
await sleep(1200)
const d1 = await page.canvasDigest()
await clickByText('定量结构')
await sleep(1200)
const d2 = await page.canvasDigest()
s.ok('定量结构开关画面 A/B/A', d0 !== d1 && d0 === d2, `${d0}→${d1}→${d2}`)

// 复盘页内容
page.reset()
await gotoResize(`${BASE}/review`)
await sleep(1500)
const rv = await page.eval('document.body.innerText.slice(0, 300)')
s.note('复盘页文本', JSON.stringify(rv))
await shot(OUT + 'qa3d-review.png')

console.log('\n' + (s.failed.length === 0 ? 'FINAL: ALL PASS' : 'FINAL: HAS FAILURES'))
process.exit(s.failed.length ? 1 : 0)
