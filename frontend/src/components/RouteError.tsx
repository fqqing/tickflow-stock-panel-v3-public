/**
 * 路由级错误兜底。
 *
 * 为什么必须有: 没有 errorElement 时, 任何渲染/effect 异常都由 React Router 的
 * 内置错误页接管 —— 整屏只剩一段堆栈, 侧栏/顶栏全部消失, 用户唯一的出路是手动刷新。
 * (真机实测踩过一次: ECharts 图表重建窗口期 getZr() 返回 null 抛错, 整个个股终端
 *  被替换成 "Unexpected Application Error", 页面里连"返回"都没有。)
 *
 * 这里给一个明确出口(重新加载 / 回首页), 并保留错误摘要方便反馈。
 */
import { isRouteErrorResponse, useRouteError } from 'react-router-dom'

export function RouteError() {
  const err = useRouteError()
  const msg = isRouteErrorResponse(err)
    ? `${err.status} ${err.statusText}`
    : err instanceof Error
      ? err.message
      : String(err ?? '未知错误')
  const stack = err instanceof Error && err.stack
    ? err.stack.split('\n').slice(0, 3).join('\n')
    : ''

  return (
    <div className="grid min-h-screen place-items-center bg-base p-6 text-foreground">
      <div className="w-full max-w-[560px] rounded-card border border-border bg-surface p-5">
        <div className="text-sm font-medium">页面出错了</div>
        <div className="mt-1 text-xs text-secondary">
          当前页面渲染中断。可以重新加载, 或回首页走别的入口 —— 其余功能不受影响。
        </div>
        <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-btn bg-elevated p-2 font-mono text-[11px] text-danger">
          {msg}
          {stack ? `\n${stack}` : ''}
        </pre>
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-btn border border-border px-3 py-1.5 text-xs transition-colors hover:bg-elevated"
          >
            重新加载
          </button>
          <a
            href="/"
            className="rounded-btn bg-accent px-3 py-1.5 text-xs text-white transition-opacity hover:opacity-90"
          >
            回首页
          </a>
        </div>
      </div>
    </div>
  )
}
