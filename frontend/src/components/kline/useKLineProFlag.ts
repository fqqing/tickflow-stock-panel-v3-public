/**
 * P1 阶段灰度开关：是否使用 KLineChart 内核。
 * 纯前端 localStorage，不触及后端 Preferences schema，方便随时回退。
 */
const KEY = 'tickflow.useKLinePro'

export function getKLineProFlag(): boolean {
  try {
    return localStorage.getItem(KEY) === 'true'
  } catch {
    return false
  }
}

export function setKLineProFlag(v: boolean) {
  try {
    localStorage.setItem(KEY, String(v))
  } catch {
    // ignore
  }
}
