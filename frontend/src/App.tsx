import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, getToken, setToken, type Json } from "./api";
import { ErrorBox, errText } from "./components/ui";
import Admin from "./pages/Admin";
import Charter from "./pages/Charter";
import Evidence from "./pages/Evidence";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Projects from "./pages/Projects";
import Quality from "./pages/Quality";
import Search from "./pages/Search";
import Screening from "./pages/Screening";
import Trace from "./pages/Trace";
import Writing from "./pages/Writing";

type Session = { user: Json; meta: Json; logout: () => void };
const SessionCtx = createContext<Session | null>(null);
export const useSession = () => useContext(SessionCtx)!;

const ProjectCtx = createContext<{ project: Json; reload: () => void } | null>(null);
export const useProject = () => useContext(ProjectCtx)!;

export default function App() {
  const [meta, setMeta] = useState<Json>(null);
  const [user, setUser] = useState<Json>(null);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);

  const loadMe = async () => {
    try {
      setUser(await api.get("/api/auth/me"));
    } catch {
      setUser(null);
    } finally {
      setChecked(true);
    }
  };
  useEffect(() => {
    api.get("/api/meta").then(setMeta).catch((e) => setError(errText(e)));
    loadMe();
  }, []);

  if (error) return <div className="center"><ErrorBox error={`无法连接后端：${error}`} /></div>;
  if (!checked || !meta) return <div className="center muted">加载中…</div>;
  if (!user) return <Login meta={meta} onLogin={loadMe} />;

  const logout = () => {
    setToken(null);
    setUser(null);
    setChecked(!meta.single_user);
    if (meta.single_user) loadMe();
  };
  return (
    <SessionCtx.Provider value={{ user, meta, logout }}>
      <Routes>
        <Route path="/" element={<Shell><Projects /></Shell>} />
        <Route path="/admin" element={<Shell><Admin /></Shell>} />
        <Route path="/p/:pid/*" element={<ProjectShell />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </SessionCtx.Provider>
  );
}

function Shell({ children, nav }: { children: React.ReactNode; nav?: React.ReactNode }) {
  const { user, meta, logout } = useSession();
  return (
    <div className="layout">
      <aside className="side">
        <div className="brand">综述协作平台</div>
        <NavLink to="/" end>我的项目</NavLink>
        {user.role === "admin" && <NavLink to="/admin">平台管理</NavLink>}
        {nav}
        <div className="group">账号</div>
        <div className="small muted" style={{ padding: "0 10px" }}>
          {user.name}（{{ student: "学生", mentor: "导师", admin: "管理员" }[user.role as string]}）<br />
          部署形态：{{ local: "单机版", server: "课题组服务器版", cloud: "云端版" }[meta.profile as string]}
          {meta.offline && " · 离线"}
        </div>
        {!meta.single_user && getToken() && <a href="#" onClick={(e) => { e.preventDefault(); logout(); }}>退出登录</a>}
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

const STAGES = [
  ["", "概览与闸门"],
  ["charter", "一 · 命题与任务书"],
  ["search", "二 · 检索策略"],
  ["screening", "三 · 文献与筛选"],
  ["evidence", "四 · 证据与大纲"],
  ["writing", "五 · 写作工作台"],
  ["quality", "六 · 核查与交付"],
  ["trace", "留痕与 AI 日志"],
];

function ProjectShell() {
  const { pid } = useParams();
  const nav = useNavigate();
  const [project, setProject] = useState<Json>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = () => api.get(`/api/projects/${pid}`).then(setProject).catch((e) => setError(errText(e)));
  useEffect(() => { reload(); }, [pid]);
  if (error) return <Shell><ErrorBox error={error} /><button onClick={() => nav("/")}>返回</button></Shell>;
  if (!project) return <Shell><div className="muted">加载中…</div></Shell>;
  const side = (
    <>
      <div className="group">{project.name}</div>
      {STAGES.map(([p, label]) => (
        <NavLink key={p} to={`/p/${pid}/${p}`} end>{label}</NavLink>
      ))}
      <div className="small muted" style={{ padding: "6px 10px" }}>
        AI 介入上限 <b>{project.ai_level_max}</b>{project.data_local_only && " · 数据不外发"}
      </div>
    </>
  );
  return (
    <ProjectCtx.Provider value={{ project, reload }}>
      <Shell nav={side}>
        <Routes>
          <Route index element={<Overview />} />
          <Route path="charter" element={<Charter />} />
          <Route path="search" element={<Search />} />
          <Route path="screening" element={<Screening />} />
          <Route path="evidence" element={<Evidence />} />
          <Route path="writing" element={<Writing />} />
          <Route path="quality" element={<Quality />} />
          <Route path="trace" element={<Trace />} />
        </Routes>
      </Shell>
    </ProjectCtx.Provider>
  );
}
