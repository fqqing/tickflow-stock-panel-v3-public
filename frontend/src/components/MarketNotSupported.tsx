// 港美股不支持提示（多市场扩展）
// 用于概念/行业/指数/市场环境/复盘等依赖 A 股专属数据或实时/付费能力的页面。
import { useMarket } from '@/lib/market'

export function MarketNotSupportedHint({
  pageName,
  reason,
  description,
  showGuide = true,
}: {
  pageName: string
  reason?: string
  /** 自定义提示文案模板；`{pageName}` 与 `{label}`（港股/美股）会被替换 */
  description?: string
  /** 是否显示「港美股可查看强度榜」引导文案，默认 true */
  showGuide?: boolean
}) {
  const { market, setMarket } = useMarket()
  const label = market === 'hk' ? '港股' : '美股'
  const [beforeLabel, afterLabel] = (description ?? '').split('{pageName}').join(pageName).split('{label}')
  return (
    <div className="p-6">
      <div className="rounded-btn border border-border bg-surface p-6 text-center">
        <div className="text-base font-medium mb-2">{pageName}</div>
        <p className="text-sm text-muted mb-4">
          {description ? (
            <>
              {beforeLabel}
              <span className="mx-1 text-accent">{label}</span>
              {afterLabel}
            </>
          ) : (
            <>
              {pageName}暂不支持
              <span className="mx-1 text-accent">{label}</span>
              {reason ? `：${reason}` : '（依赖 A 股专属数据或付费/实时能力）'}
            </>
          )}
        </p>
        {showGuide && (
          <p className="text-xs text-muted mb-4">
            港美股可查看「看板」总览与「连板梯队」页的<strong>强度榜</strong>（新高突破 / 动量 / 放量）。
          </p>
        )}
        <button
          onClick={() => setMarket('cn')}
          className="h-8 px-4 rounded-btn bg-accent/10 text-accent text-xs font-medium hover:bg-accent/20 cursor-pointer"
        >
          切换到 A 股
        </button>
      </div>
    </div>
  )
}
