/**
 * QA 通用 CDP 测试底座（零第三方依赖）。
 *
 * 为什么自己写而不用 agent-browser: 本机 agent-browser 不可用, 直接 CDP 最稳。
 * 复用方式: import { openPage } from './qa-harness.mjs'
 *
 * 能力:
 *   - 打开 URL / 等待就绪
 *   - 收集 console(error|warning) / JS 异常 / 网络 4xx5xx / 失败请求
 *   - 键盘输入(单字符按 code 派发, 与浏览器真实按键一致)
 *   - 截图、视口尺寸切换、DOM 求值
 *   - 断言收集器 + 汇总判定
 */
const CDP = process.env.TF_CDP || 'http://127.0.0.1:9222'

export const sleep = ms => new Promise(r => setTimeout(r, ms))

export class Page {
  constructor(ws, meta) {
    this.ws = ws
    this.meta = meta
    this.nextId = 1
    this.pending = new Map()
    this.consoleErrors = []
    this.consoleWarnings = []
    this.exceptions = []
    this.netBad = []   // 4xx / 5xx
    this.netFailed = [] // 网络层失败
    this.reqMap = new Map()
    ws.onmessage = ev => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        this.pending.get(msg.id)(msg); this.pending.delete(msg.id); return
      }
      const m = msg.method
      if (m === 'Runtime.consoleAPICalled') {
        const t = msg.params?.type
        const txt = (msg.params?.args || []).map(a => a.value ?? a.description ?? '').join(' ').slice(0, 300)
        if (t === 'error') this.consoleErrors.push(txt)
        else if (t === 'warning' || t === 'warn') this.consoleWarnings.push(txt)
      } else if (m === 'Runtime.exceptionThrown') {
        const d = msg.params?.exceptionDetails
        this.exceptions.push(String(d?.exception?.description || d?.text || '').split('\n').slice(0, 2).join(' | '))
      } else if (m === 'Network.requestWillBeSent') {
        this.reqMap.set(msg.params.requestId, msg.params.request.url)
      } else if (m === 'Network.responseReceived') {
        const s = msg.params.response.status
        if (s >= 400) this.netBad.push({ status: s, url: msg.params.response.url })
      } else if (m === 'Network.loadingFailed') {
        const url = this.reqMap.get(msg.params.requestId) || '?'
        this.netFailed.push({ err: msg.params.errorText, url })
      }
    }
  }

  send(method, params = {}) {
    return new Promise(res => {
      const id = this.nextId++
      this.pending.set(id, res)
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }

  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: typeof expr === 'string' ? expr : `(${expr.toString()})()`,
      returnByValue: true, awaitPromise: true,
    })
    if (r.result?.exceptionDetails) {
      const d = r.result.exceptionDetails
      throw new Error(String(d.exception?.description || d.text || 'eval failed').split('\n')[0])
    }
    return r.result?.result?.value
  }

  /** 单字符按键: 同时给 key/text/code, 保证 React onKeyDown 收到正确 e.key */
  async key(k) {
    const code = k.length === 1 ? `Key${k.toUpperCase()}` : k
    const common = { key: k, code, windowsVirtualKeyCode: k.length === 1 ? k.toUpperCase().charCodeAt(0) : undefined }
    await this.send('Input.dispatchKeyEvent', { type: 'keyDown', text: k.length === 1 ? k : undefined, ...common })
    await this.send('Input.dispatchKeyEvent', { type: 'keyUp', ...common })
  }

  /**
   * 导航并**确认真的落地了**。
   * ★ 教训: 上一个页面卡在"载入中/持续请求"时, Page.navigate 会静默不生效,
   *   后续所有断言就跑在**上一个标的**上 —— 全绿或全红都是假的。
   */
  async goto(url, waitMs = 12000) {
    const want = url.replace(/#.*$/, '')
    for (let attempt = 0; attempt < 2; attempt++) {
      await this.send('Page.navigate', { url })
      const t0 = Date.now()
      while (Date.now() - t0 < waitMs) {
        await sleep(250)
        const s = await this.eval('({ u: location.href.replace(/#.*$/,""), r: document.readyState })').catch(() => null)
        if (s && s.u === want && s.r === 'complete') return true
      }
      // 还没落地就打断当前加载再试一次
      await this.send('Page.stopLoading').catch(() => {})
    }
    return false
  }
  async reload() { await this.send('Page.reload') }

  /** 当前页面 URL 上的标的(校验测试跑在对的标的上) */
  async currentSymbol() {
    return this.eval('decodeURIComponent((location.pathname.match(/\\/stock\\/(.+)$/) || [])[1] || "")')
  }

  /** 主图区域「有内容」的像素比例: 用来验证 canvas 真的画了东西而不是空白 */
  async inkRatio() {
    return this.eval(`(() => {
      const cv = [...document.querySelectorAll('canvas')].sort((a,b) => b.width*b.height - a.width*a.height)[0]
      if (!cv) return -1
      try {
        const ctx = cv.getContext('2d')
        if (!ctx) return -2
        const d = ctx.getImageData(0, 0, cv.width, cv.height).data
        let ink = 0, total = 0
        for (let i = 3; i < d.length; i += 4 * 53) { total++; if (d[i] > 8) ink++ }
        return total ? +(ink / total).toFixed(4) : -3
      } catch (e) { return -4 }
    })()`)
  }

  async resize(w, h = 900) {
    await this.send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: false })
    await sleep(350)
  }

  async shot(file) {
    const r = await this.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
    if (!r.result?.data) return null
    const { writeFileSync } = await import('node:fs')
    writeFileSync(file, Buffer.from(r.result.data, 'base64'))
    return file
  }

  /**
   * 强制指定 K 线内核。
   * ★ 键名是 `tickflow.useKLinePro`(见 useKLineProFlag.ts)。
   *   老脚本里写的 `tickflow-kline-pro` 是错的 —— 于是"锁定内核"从未生效,
   *   测试实际跑在哪个内核上完全取决于上一次留下的状态。
   */
  async setKernel(kind) {
    const v = kind === 'klinepro' ? 'true' : 'false'
    await this.eval(`localStorage.setItem('tickflow.useKLinePro','${v}')`)
  }

  /** 当前内核: 读顶栏那个切换按钮的文案(显示的就是当前内核名) */
  async kernel() {
    return this.eval(`(() => {
      const b = [...document.querySelectorAll('button')].find(x => /^(ECharts|KLinePro)$/.test((x.innerText||'').trim()))
      return b ? b.innerText.trim() : 'unknown'
    })()`)
  }

  /** 主图区域所有 canvas 的内容指纹: 用于断言「画面真的变了」 */
  async canvasDigest() {
    // 在浏览器内算内容指纹: 采样 dataURL 字符, 避免把几百 KB 传回 node。
    // 只看「长度」是不够的 —— 两次不同的画面长度常常相同, 会假报「没变化」。
    const data = await this.eval(`(() => {
      const out = []
      for (const c of document.querySelectorAll('canvas')) {
        if (!c.width || !c.height) continue
        try {
          const s = c.toDataURL('image/png')
          let h = 0
          for (let i = 0; i < s.length; i += 3) h = (h * 33 + s.charCodeAt(i)) | 0
          out.push(c.width + 'x' + c.height + ':' + s.length + ':' + (h >>> 0).toString(36))
        } catch { out.push('x') }
      }
      return out.join('|')
    })()`)
    const { createHash } = await import('node:crypto')
    return createHash('md5').update(String(data)).digest('hex').slice(0, 8)
  }

  /** 清掉已收集的噪声, 用于「分阶段断言」 */
  reset() {
    this.consoleErrors.length = 0
    this.consoleWarnings.length = 0
    this.exceptions.length = 0
    this.netBad.length = 0
    this.netFailed.length = 0
  }
}

export async function openPage() {
  const list = await (await fetch(`${CDP}/json/list`)).json()
  const target = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl)
  if (!target) throw new Error('CDP 上没有 page target, 先起 Edge(--remote-debugging-port=9222)')
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
  const page = new Page(ws, target)
  await page.send('Page.enable')
  await page.send('Runtime.enable')
  await page.send('Log.enable')
  await page.send('Network.enable')
  await page.send('DOM.enable')
  return page
}

/** 断言收集器 */
export class Suite {
  constructor(title) {
    this.title = title
    this.items = []
  }
  ok(name, cond, detail = '') {
    this.items.push({ name, pass: !!cond, detail })
    console.log(`  ${cond ? '[PASS]' : '[FAIL]'} ${name}${detail ? '  ' + detail : ''}`)
    return !!cond
  }
  /** 已知问题/观察项: 不计入判定, 但进报告 */
  note(name, detail) {
    this.items.push({ name, pass: null, detail })
    console.log(`  [NOTE] ${name}  ${detail}`)
  }
  get failed() { return this.items.filter(i => i.pass === false) }
  get passed() { return this.items.filter(i => i.pass === true) }
  summary() {
    const f = this.failed.length
    console.log(`\n[${this.title}] 通过 ${this.passed.length} / 失败 ${f}`)
    if (f) console.log('  失败项: ' + this.failed.map(i => `${i.name}(${i.detail})`).join('; '))
    return f === 0
  }
}
