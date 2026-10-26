import { lazy } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Onboarding } from './pages/Onboarding'
import { Auth } from './pages/Auth'
import { useSettings } from './lib/useSharedQueries'
import { useQuery } from '@tanstack/react-query'
import { api } from './lib/api'
import { Logo } from './components/Logo'
import { ExtensionBoundary } from './extensions/ExtensionBoundary'
import { RouteError } from './components/RouteError'
import {
  finalizeFrontendExtensions,
  getFrontendExtensionLoadErrors,
  getFrontendExtensionRoutes,
} from './extensions/registry'

// 代码分割: 页面全部 lazy 加载, 避免首屏打包所有页面 (ECharts / klinecharts /
// framer-motion 等重库) → 大幅减小首屏 bundle。命名导出用 .then 映射为 default。
// Layout / Onboarding / Auth 为应用外壳与入口, 保持同步加载。
const Watchlist = lazy(() => import('./pages/Watchlist').then(m => ({ default: m.Watchlist })))
const Screener = lazy(() => import('./pages/Screener').then(m => ({ default: m.Screener })))
const ChanScan = lazy(() => import('./pages/ChanScan').then(m => ({ default: m.ChanScan })))
const Backtest = lazy(() => import('./pages/Backtest').then(m => ({ default: m.Backtest })))
const Mining = lazy(() => import('./pages/Mining').then(m => ({ default: m.Mining })))
const Financials = lazy(() => import('./pages/Financials').then(m => ({ default: m.Financials })))
const Data = lazy(() => import('./pages/Data').then(m => ({ default: m.Data })))
const Monitor = lazy(() => import('./pages/Monitor').then(m => ({ default: m.Monitor })))
const Dashboard = lazy(() => import('./pages/Dashboard').then(m => ({ default: m.Dashboard })))
const AnalysisDetail = lazy(() => import('./pages/AnalysisDetail').then(m => ({ default: m.AnalysisDetail })))
const ConceptAnalysis = lazy(() => import('./pages/ConceptAnalysis').then(m => ({ default: m.ConceptAnalysis })))
const IndustryAnalysis = lazy(() => import('./pages/IndustryAnalysis').then(m => ({ default: m.IndustryAnalysis })))
const StockAnalysis = lazy(() => import('./pages/StockAnalysis').then(m => ({ default: m.StockAnalysis })))
const StockTerminal = lazy(() => import('./pages/StockTerminal').then(m => ({ default: m.StockTerminal })))
const News = lazy(() => import('./pages/News').then(m => ({ default: m.News })))
const Review = lazy(() => import('./pages/Review').then(m => ({ default: m.Review })))
const LimitUpLadder = lazy(() => import('./pages/LimitUpLadder').then(m => ({ default: m.LimitUpLadder })))
const Mainline = lazy(() => import('./pages/Mainline').then(m => ({ default: m.Mainline })))
const Branding = lazy(() => import('./pages/Branding').then(m => ({ default: m.Branding })))
const Settings = lazy(() => import('./pages/Settings').then(m => ({ default: m.Settings })))
const Users = lazy(() => import('./pages/Users').then(m => ({ default: m.Users })))
const Indices = lazy(() => import('./pages/Indices').then(m => ({ default: m.Indices })))
const Regime = lazy(() => import('./pages/Regime').then(m => ({ default: m.Regime })))
const AbnormalMoves = lazy(() => import('./pages/AbnormalMoves').then(m => ({ default: m.AbnormalMoves })))
const SignalLab = lazy(() => import('./pages/SignalLab').then(m => ({ default: m.SignalLab })))
const SignalCatalog = lazy(() => import('./pages/SignalCatalog').then(m => ({ default: m.SignalCatalog })))
const Pulse = lazy(() => import('./pages/Pulse').then(m => ({ default: m.Pulse })))
const DailyPicks = lazy(() => import('./pages/DailyPicks').then(m => ({ default: m.DailyPicks })))
const Dev = lazy(() => import('./pages/Dev').then(m => ({ default: m.Dev })))

const CORE_ROUTE_PATHS = new Set([
  '/',
  '/onboarding',
  '/login',
  '/overview',
  '/analysis',
  '/analysis/:menuId',
  '/concept-analysis',
  '/industry-analysis',
  '/stock-analysis',
  '/review',
  '/watchlist',
  '/screener',
  '/chan-scan',
  '/backtest',
  '/mining',
  '/financials',
  '/data',
  '/monitor',
  '/limit-ladder',
  '/mainline',
  '/indices',
  '/regime',
  '/abnormal',
  '/signal-lab',
  '/signal-catalog',
  '/branding',
  '/settings',
  '/users',
  '/dev',
  '/daily-picks',
  '/settings/keys',
  '/settings/ai',
  '/settings/queries',
])

finalizeFrontendExtensions(CORE_ROUTE_PATHS)
const frontendExtensionRoutes = getFrontendExtensionRoutes()
const frontendExtensionErrors = getFrontendExtensionLoadErrors()
if (frontendExtensionErrors.length > 0) {
  console.error('部分前端扩展加载失败', frontendExtensionErrors)
}

// 首次使用守卫 —— 未完成向导则重定向到 /onboarding
// 只挂在根路由上;/onboarding 本身不被守卫,避免循环重定向。
// settings 由 Layout 预取,守卫判定不产生额外请求。
function OnboardingGuard({ children }: { children: React.ReactNode }) {
  const settings = useSettings()

  // 仅首次加载(本地无缓存)时显示占位。
  // 后台重取 (isFetching) 时本地已有上一份缓存可用, 直接放行, 避免切页时整屏 logo 闪烁。
  // 防误重定向已由 Onboarding/AI 等处 invalidate 前的 setQueryData 同步缓存兜底。
  if (settings.isLoading) {
    return (
      <div className="min-h-screen bg-base grid place-items-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <Logo size={28} className="text-foreground" />
          <div className="text-xs">加载中…</div>
        </div>
      </div>
    )
  }

  // 查询出错或字段缺失时不拦截 —— 宁可放行,也不把用户卡在空白页
  if (settings.data && settings.data.onboarding_completed === false) {
    return <Navigate to="/onboarding" replace />
  }

  return <>{children}</>
}

// 认证守卫 —— 已设密码但未登录时立即跳登录页。
// 之前依赖数据请求 401 + react-query 重试兜底跳转: api.settings 被 retry 3 次
// (每次间隔递增) 才触发全局 onError 跳 /login, 造成「未登录先看到主界面外壳、
// 再等 5-9 秒才弹登录页」的假象。这里用 auth-status(白名单, 恒 200) 主动判定,
// 未登录立即重定向, 不依赖重试。
function AuthGuard({ children }: { children: React.ReactNode }) {
  const auth = useQuery({ queryKey: ['auth-status'], queryFn: api.authStatus })

  if (auth.isLoading) {
    return (
      <div className="min-h-screen bg-base grid place-items-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <Logo size={28} className="text-foreground" />
          <div className="text-xs">加载中…</div>
        </div>
      </div>
    )
  }

  // 未设密码(configured=false)：放行，交给 Onboarding/OnboardingGuard 处理首次设置
  if (auth.data && auth.data.configured === false) {
    return <>{children}</>
  }

  // 已设密码但未登录：立即跳登录页(带 redirect 回跳)
  if (auth.data && auth.data.authenticated === false) {
    const redirect = encodeURIComponent(window.location.pathname + window.location.search)
    return <Navigate to={`/login?redirect=${redirect}`} replace />
  }

  // 已登录：放行
  return <>{children}</>
}

export const router = createBrowserRouter([
  { path: '/onboarding', element: <Onboarding /> },
  { path: '/login', element: <Auth /> },
  {
    path: '/',
    element: (
      <AuthGuard>
        <OnboardingGuard>
          <Layout />
        </OnboardingGuard>
      </AuthGuard>
    ),
    // 渲染异常兜底。不给的话 React Router 会用内置错误页整屏替换(只剩堆栈, 无出口)。
    // 放在根路由上, 子路由自动继承。
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'overview', element: <Navigate to="/" replace /> },
      { path: 'analysis', element: <Navigate to="/settings?tab=ext-pages" replace /> },
      { path: 'analysis/:menuId', element: <AnalysisDetail /> },
      { path: 'concept-analysis', element: <ConceptAnalysis /> },
      { path: 'industry-analysis', element: <IndustryAnalysis /> },
      { path: 'stock-analysis', element: <StockAnalysis /> },
      // 个股终端(全屏工作区): 从自选/监控/异动/选股点进来, 占满主区而非弹窗
      { path: 'stock/:symbol', element: <StockTerminal /> },
      { path: 'news', element: <News /> },
      { path: 'review', element: <Review /> },
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'screener', element: <Screener /> },
      { path: 'chan-scan', element: <ChanScan /> },
      { path: 'backtest', element: <Backtest /> },
      { path: 'mining', element: <Mining /> },
      { path: 'financials', element: <Financials /> },
      { path: 'data', element: <Data /> },
      { path: 'monitor', element: <Monitor /> },
      { path: 'limit-ladder', element: <LimitUpLadder /> },
      { path: 'mainline', element: <Mainline /> },
      { path: 'indices', element: <Indices /> },
    { path: 'regime', element: <Regime /> },
      { path: 'abnormal', element: <AbnormalMoves /> },
      { path: 'signal-lab', element: <SignalLab /> },
      { path: 'signal-catalog', element: <SignalCatalog /> },
      { path: 'pulse', element: <Pulse /> },
      { path: 'daily-picks', element: <DailyPicks /> },
      { path: 'branding', element: <Branding /> },
      { path: 'settings', element: <Settings /> },
      { path: 'users', element: <Users /> },
      // 隐藏路由：开发者工具（不暴露在菜单，仅供调试）
      { path: 'dev', element: <Dev /> },
      // 旧路由兼容重定向
      { path: 'settings/keys', element: <Navigate to="/settings?tab=data-sources" replace /> },
      { path: 'settings/ai', element: <Navigate to="/settings?tab=ai" replace /> },
      { path: 'settings/queries', element: <Navigate to="/settings?tab=queries" replace /> },
      ...frontendExtensionRoutes.map(route => {
        const ExtensionPage = route.component
        return {
          path: route.path.slice(1),
          element: (
            <ExtensionBoundary extensionId={route.extensionId}>
              <ExtensionPage />
            </ExtensionBoundary>
          ),
        }
      }),
    ],
  },
])
