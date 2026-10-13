/**
 * 用户管理页 — 仅管理员可见。
 *
 * 功能:
 *   - 列出所有用户(用户名 / 是否管理员 / 创建时间)
 *   - 创建用户(用户名 + 密码, 可选管理员)
 *   - 删除用户(主管理员不可删, 至少保留一个管理员)
 *   - 重置用户密码
 */
import { useState, type FormEvent } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Plus, Trash2, KeyRound, ShieldCheck, User as UserIcon, AlertCircle } from 'lucide-react'
import { api } from '@/lib/api'

export function Users() {
  const qc = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [err, setErr] = useState('')
  const [resetTarget, setResetTarget] = useState<string | null>(null)
  const [resetPwd, setResetPwd] = useState('')
  const [resetErr, setResetErr] = useState('')

  const auth = useQuery({ queryKey: ['auth-status'], queryFn: api.authStatus })
  const usersQ = useQuery({ queryKey: ['auth-users'], queryFn: api.listUsers })

  const createMut = useMutation({
    mutationFn: () => api.createUser(username, password, isAdmin),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['auth-users'] })
      setUsername(''); setPassword(''); setIsAdmin(false); setErr('')
    },
    onError: (e: any) => setErr(e?.message || '创建失败'),
  })

  const deleteMut = useMutation({
    mutationFn: (u: string) => api.deleteUser(u),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['auth-users'] }),
    onError: (e: any) => setErr(e?.message || '删除失败'),
  })

  const resetMut = useMutation({
    mutationFn: () => api.resetUserPassword(resetTarget!, resetPwd),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['auth-users'] })
      setResetTarget(null); setResetPwd(''); setResetErr('')
    },
    onError: (e: any) => setResetErr(e?.message || '重置失败'),
  })

  if (auth.isLoading) {
    return <div className="grid min-h-[50vh] place-items-center text-muted"><Loader2 className="h-5 w-5 animate-spin" /></div>
  }
  if (!auth.data?.is_admin) {
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center gap-2 text-muted">
        <ShieldCheck className="h-8 w-8" />
        <div className="text-sm">需要管理员权限才能访问此页</div>
      </div>
    )
  }

  const users = usersQ.data?.users ?? []
  const me = auth.data?.username

  const handleCreate = (e: FormEvent) => {
    e.preventDefault()
    setErr('')
    if (username.length < 3) { setErr('用户名至少 3 位'); return }
    if (password.length < 6) { setErr('密码至少 6 位'); return }
    createMut.mutate()
  }

  const handleReset = (e: FormEvent) => {
    e.preventDefault()
    setResetErr('')
    if (resetPwd.length < 6) { setResetErr('密码至少 6 位'); return }
    resetMut.mutate()
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="mb-6">
        <h1 className="text-lg font-semibold text-foreground">用户管理</h1>
        <p className="mt-1 text-xs text-muted">管理可访问面板的账号。每个账号的数据(自选股/设置/AI 报告等)互相隔离。</p>
      </div>

      {/* 创建用户 */}
      <div className="mb-6 rounded-card border border-border bg-surface/60 p-4">
        <div className="mb-3 text-sm font-medium text-foreground">创建账号</div>
        <form onSubmit={handleCreate} className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={username}
            onChange={e => setUsername(e.target.value)}
            placeholder="用户名(字母开头, 3-32 位)"
            className="h-9 w-52 rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent/50"
          />
          <input
            type="text"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="初始密码(至少 6 位)"
            className="h-9 w-44 rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent/50"
          />
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <input type="checkbox" checked={isAdmin} onChange={e => setIsAdmin(e.target.checked)} className="accent-accent" />
            管理员
          </label>
          <button
            type="submit"
            disabled={createMut.isPending}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
          >
            {createMut.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            创建
          </button>
        </form>
        {err && (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-danger"><AlertCircle className="h-3.5 w-3.5" />{err}</div>
        )}
      </div>

      {/* 用户列表 */}
      <div className="overflow-hidden rounded-card border border-border">
        {usersQ.isLoading ? (
          <div className="flex items-center justify-center py-10 text-muted"><Loader2 className="h-5 w-5 animate-spin" /></div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted">
                <th className="px-4 py-2.5 font-medium">用户名</th>
                <th className="px-4 py-2.5 font-medium">角色</th>
                <th className="px-4 py-2.5 font-medium">创建时间</th>
                <th className="px-4 py-2.5 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.username} className="border-b border-border/60 last:border-0">
                  <td className="px-4 py-2.5">
                    <span className="inline-flex items-center gap-1.5 text-foreground">
                      <UserIcon className="h-3.5 w-3.5 text-muted" />
                      {u.username}
                      {u.username === me && <span className="text-[10px] text-muted">(我)</span>}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    {u.is_admin
                      ? <span className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-0.5 text-[11px] text-accent"><ShieldCheck className="h-3 w-3" />管理员</span>
                      : <span className="text-muted">普通用户</span>}
                  </td>
                  <td className="px-4 py-2.5 text-muted">
                    {u.created_at ? new Date(u.created_at * 1000).toLocaleDateString() : '—'}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => { setResetTarget(u.username); setResetPwd(''); setResetErr('') }}
                        title="重置密码"
                        className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => { if (confirm(`确定删除用户「${u.username}」？`)) deleteMut.mutate(u.username) }}
                        disabled={u.username === me}
                        title={u.username === me ? '不能删除自己' : '删除用户'}
                        className="rounded-btn p-1.5 text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-30"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 重置密码弹窗 */}
      {resetTarget && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" onClick={() => setResetTarget(null)}>
          <form
            onSubmit={handleReset}
            onClick={e => e.stopPropagation()}
            className="w-full max-w-sm rounded-card border border-border bg-surface p-5"
          >
            <div className="mb-3 text-sm font-medium text-foreground">重置「{resetTarget}」的密码</div>
            <input
              type="text"
              value={resetPwd}
              onChange={e => setResetPwd(e.target.value)}
              placeholder="新密码(至少 6 位)"
              autoFocus
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent/50"
            />
            {resetErr && <div className="mt-2 flex items-center gap-1.5 text-xs text-danger"><AlertCircle className="h-3.5 w-3.5" />{resetErr}</div>}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={() => setResetTarget(null)} className="h-9 rounded-btn px-3 text-sm text-muted hover:text-foreground">取消</button>
              <button type="submit" disabled={resetMut.isPending} className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-50">
                {resetMut.isPending && <Loader2 className="h-4 w-4 animate-spin" />}确定
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
