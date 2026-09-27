import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, waitJob, type Json } from "../api";

export function useLoad<T = Json>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api.get(path));
      setError(null);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

export function errText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail;
    if (typeof d === "string") return d;
    if (d?.message) return d.message + (d.errors ? "\n" + d.errors.join("\n") : "") + (d.validation ? "\n" + d.validation.issues.map((i: Json) => `· ${i.message} ${i.fragment}`).join("\n") : "");
    return JSON.stringify(d);
  }
  return String(e);
}

/** 包装一个异步动作：自动处理 loading 与错误显示。 */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(errText(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, setError };
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="err">{error}</div> : null;
}

export function Pill({ kind, children }: { kind?: "ok" | "warn" | "bad" | "info"; children: ReactNode }) {
  return <span className={`pill ${kind || ""}`}>{children}</span>;
}

export function Card({ title, children, right }: { title?: ReactNode; children: ReactNode; right?: ReactNode }) {
  return (
    <div className="card">
      {(title || right) && (
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>{title}</h3>
          <div className="row">{right}</div>
        </div>
      )}
      {children}
    </div>
  );
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="stat">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

/** 运行一个通用 Prompt（阶段一分析等），结果以“AI 建议”底色显示。 */
export function AiTask({ pid, task, label, params, hint }: { pid: string; task: string; label: string; params?: Json; hint?: string }) {
  const { busy, error, run } = useAction();
  const [out, setOut] = useState<Json>(null);
  const [prompt, setPrompt] = useState<string | null>(null);
  return (
    <div style={{ marginBottom: 12 }}>
      <div className="row">
        <button className="primary" disabled={busy} onClick={() => run(async () => setOut(await api.post(`/api/projects/${pid}/ai/${task}`, { params: params || {} })))}>
          {busy ? "运行中…" : `${task} ${label}`}
        </button>
        <button disabled={busy} onClick={() => run(async () => { const r = await api.post(`/api/projects/${pid}/ai/${task}/render`, { params: params || {} }); setPrompt(r.prompt); })}>
          导出 Prompt（网页版 AI）
        </button>
        {hint && <span className="muted small">{hint}</span>}
      </div>
      <ErrorBox error={error} />
      {prompt && (
        <div className="card" style={{ marginTop: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="muted small">复制到网页版 AI 使用；结果请人工核对</span>
            <div className="row">
              <button className="small" onClick={() => navigator.clipboard?.writeText(prompt)}>复制</button>
              <button className="small" onClick={() => setPrompt(null)}>关闭</button>
            </div>
          </div>
          <pre className="md small">{prompt}</pre>
        </div>
      )}
      {out && (
        <div className="ai" style={{ marginTop: 8 }}>
          <div className="muted small">{out.provider}/{out.model} · {out.run_id}</div>
          <pre className="md">{typeof out.data === "string" || !out.data ? out.text : JSON.stringify(out.data, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

/** 启动后台任务并显示进度。 */
export function JobButton({ label, start, onDone, disabled }: { label: string; start: () => Promise<Json>; onDone?: (j: Json) => void; disabled?: boolean }) {
  const [prog, setProg] = useState<Json>(null);
  const { busy, error, run } = useAction();
  return (
    <span>
      <button disabled={busy || disabled} onClick={() => run(async () => {
        const job = await start();
        const res = job?.id && job.kind ? await waitJob(job.id, setProg) : job;
        if (res?.status === "failed") throw new Error(res.error);
        onDone?.(res);
      })}>
        {busy ? `进行中 ${prog?.progress?.done ?? 0}/${prog?.progress?.total ?? "?"}` : label}
      </button>
      {error && <ErrorBox error={error} />}
    </span>
  );
}

export function LevelBadge({ level }: { level: string | null | undefined }) {
  const k = level === "A" ? "ok" : level === "B" ? "info" : level === "U" ? "warn" : level === "D" ? "bad" : undefined;
  return level ? <Pill kind={k}>{level}</Pill> : <span className="muted">—</span>;
}

export const DECISION_ZH: Record<string, string> = { include: "纳入", exclude: "排除", uncertain: "不确定" };
export const SOURCE_ZH: Record<string, string> = { human: "人工", ai_then_human: "AI 预判后人工确认", ai_unreviewed: "AI 预判未复核" };
export const VERDICT_ZH: Record<string, string> = { strong: "强匹配", partial: "部分匹配", none: "不匹配 / 无依据", undetermined: "无法判断", na: "N/A 作者观点" };
