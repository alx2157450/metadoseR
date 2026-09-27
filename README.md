# 综述协作平台 ReviewDesk

人机协同撰写高水平医学综述的平台，按《人机协同撰写高水平医学综述：完整操作细则与“综述协作平台”开发规范 v2.0》实现：

- **以导师命题为起点**：《综述任务书》结构化表单、版本管理，导师批准（G0）后锁定；
- **研究者主导写作**：正文由学生撰写，AI 只提供检索、证据包、论证骨架与审阅意见；平台**有意不提供** AI 直接生成段落的功能；
- **AI 介入级别 L0–L3** 由导师设定，学生不能自行调高；
- **六道人工闸门 G0–G5**、**全程留痕**（每次 AI 调用记录模型、Prompt 版本、采样参数与输入输出哈希；审计日志哈希链防篡改）；
- **同一套代码，三种部署形态**：单机版（可完全离线）、课题组服务器版（数据不出内网）、云端版（多租户）。

> 本仓库原为 `metadoseR`（剂量-反应 Meta 分析 R 包）的占位仓库，目前包含综述协作平台的全部代码。

## 快速开始

### 单机版（学生个人电脑）

**Docker（推荐）**

```bash
docker compose -f deploy/docker-compose.local.yml up -d --build
# 打开 http://localhost:8000 （单用户模式，免登录）
# 同时在本机运行 Ollama 开源模型：追加 --profile ollama
```

**不用 Docker**（Python ≥ 3.10、Node ≥ 20）

```bash
./scripts/run-local.sh                                   # macOS / Linux
powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1   # Windows
```

数据保存在 SQLite + 本地文件夹（脚本方式为 `data/`，Docker 方式为卷 `reviewdesk-data`），随时可在界面中“导出项目包”发给导师或备份。无任何模型时，平台使用内置的**离线规则引擎**跑通全流程；在“平台管理 → 模型网关”中接入 DeepSeek 等云端 API 或本机 Ollama 后即可使用大模型。

### 课题组服务器版（PostgreSQL + S3 对象存储 + 独立 worker）

```bash
cp deploy/.env.example deploy/.env      # 修改数据库密码、密钥、初始管理员密码
docker compose -f deploy/docker-compose.server.yml --env-file deploy/.env up -d --build
# 可选：--profile grobid（PDF 解析）  --profile gpu（vLLM 部署开源模型，需 NVIDIA GPU）
```

用初始管理员登录后，在“平台管理”中创建学生 / 导师账号。纯内网部署设置 `REVIEWDESK_OFFLINE=true`：禁止一切外部请求，并强制只用本地模型。

### 云端版（Kubernetes，托管数据库与对象存储）

```bash
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml   # 填写托管 PostgreSQL、S3、密钥
kubectl apply -k deploy/k8s
```

`app` 与 `worker` 都可水平扩展（worker 在 PostgreSQL 上用 `SKIP LOCKED` 抢占任务）。镜像：`docker build -t <registry>/reviewdesk:<tag> .`。

## 部署形态对照

| | 单机版 | 课题组服务器版 | 云端版 |
|---|---|---|---|
| `REVIEWDESK_PROFILE` | `local` | `server` | `cloud` |
| 关系库 | SQLite | PostgreSQL（pgvector 镜像） | 托管 PostgreSQL |
| 对象存储 | 本地文件夹 | S3 兼容（默认 SeaweedFS，可换 MinIO） | 云厂商 S3 |
| 后台任务 | 进程内线程 | 独立 worker 容器 | 多副本 worker |
| 账号 | 单用户免登录 | 学生 / 导师 / 管理员 | 同左，按项目隔离 |
| 模型 | 云端 API 或本机 Ollama | vLLM 本地模型 + 可选云端 API | 云端 API + 托管推理 |

每个项目可由导师标记为**“数据不外发”**：平台强制该项目只调用本地模型（未配置本地模型时退回离线规则引擎，绝不外发）。

## 功能模块（规范第九部分）

| 模块 | 内容 |
|---|---|
| A 项目与命题 | 命题录入、任务书表单与版本、G0 审批、AI 介入级别、P01–P05（题目解构、问题清单、摸底、重叠分析、切入角度）、快速摸底统计与领域成熟度判断 |
| B 检索 | P06 检索式生成、**PubMed 语法校验器**（拦截 Scopus / WoS / Embase / 知网语法、`?` 通配符、短截词、邻近检索错误）、多库转换（规则版 + P07）、MeSH 核实、种子文献召回检验、随机 30 条查准率抽查 + P09 误检诊断、PRISMA-S 检索日志、引文追溯（OpenAlex）、预印本（Europe PMC） |
| C 文献库与筛选 | PubMed 拉取与 .nbib / RIS / BibTeX / 知网导入、PMID / DOI 精确 + 标题模糊去重、Crossref 元数据与撤稿、**筛选校准**（盲评、敏感度 / 特异度 / κ、分歧列表）、AI 辅助筛选（P10，按上下文自动分批、依据句必须出自摘要）、主动学习排序、排除项 ≥ 20% 抽查（发现漏检自动退回校准轮）、系统综述双人独立筛选、网页版 AI 兼容（导出分批文本 / 回填 JSON）、全文获取与解析（PMC / Europe PMC / 上传 / GROBID）、Zotero Web API 同步、PRISMA 数据 |
| D 证据与大纲 | P11 证据卡抽取（引句逐字校验）、原文 / 证据卡左右对照核对、**学生先写大纲**（初版提交前不显示证据地图）、证据地图与薄弱节点、P12 对照诊断、P13 多视角提问、初版与定稿差异、章节文献包、G3 |
| E 写作工作台 | 段落证据包、P16 论证骨架（L2，只给要点）、P15 草稿审阅（L2）、LANG 语言建议（L3，修订模式逐条接受）、**文本来源标记**（人工 / 接受的 AI 建议 / 外部粘贴）与人工撰写占比、版本历史、P14 段落规划、P17 / P18 / P20 |
| F 质量与交付 | P19 断言—引文核查（段落修改后自动退回待核查）、导师 G4 抽查、引文真实性与撤稿报告、SANRA 与检查清单、P21 AI 使用声明（由日志汇总起草）、导出 Word（Pandoc）/ Markdown / BibTeX / 检索日志 / 核查报告 / 项目包 |
| X 平台基础 | 用户与角色、模型网关（OpenAI 兼容接口：DeepSeek、通义千问、OpenAI、LiteLLM 代理、Ollama、vLLM；按任务路由、本地回退、密钥加密存储、预算上限）、Prompt 注册表（P01–P21 + LANG，语义化版本、JSON Schema 校验与自动重试）、审计日志、项目包导入导出、用量与成本 |

规范条目与实现的逐项对照、以及尚未实现的部分，见 [docs/spec-mapping.md](docs/spec-mapping.md)。

## 开发

```bash
# 后端
cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                       # 单元测试 + 六阶段端到端流程测试
.venv/bin/reviewdesk serve --port 8000    # API：http://127.0.0.1:8000/docs

# 前端（开发模式自动代理到 8000）
cd frontend && npm ci && npm run dev      # http://localhost:5173
npm test && npm run build
```

目录结构：

```
backend/reviewdesk/
  api/            REST 接口（projects、search、library、evidence、writing、quality、auth）
  gateway/        模型网关、Prompt 注册表加载与校验、离线规则引擎
  prompts/        registry.yaml —— P01–P21 版本化 Prompt
  services/       PubMed / Europe PMC / Crossref / OpenAlex 连接器、检索语法、导入去重、筛选指标、全文解析、Zotero、导出
  pipelines.py    批量 AI 任务与外部数据任务
  jobs.py         数据库队列（单机内嵌 / 服务器独立 worker）
frontend/src/     React + TypeScript 界面
deploy/           Docker Compose（单机、服务器）与 Kubernetes（云端）
```

主要环境变量见 `backend/reviewdesk/config.py` 与 `deploy/.env.example`（前缀 `REVIEWDESK_`）。

## 合规提示

- 受版权保护的全文仅供上传者本人解析使用，不在用户间共享，也不进入项目包；
- 期刊指标请导入机构已授权的指标表，只作参考，不作为纳入依据；
- 在中国境内面向公众提供生成式 AI 服务须遵守《生成式人工智能服务管理暂行办法》等规定；跨境部署需评估数据出境要求；
- 依赖许可：核心依赖均为 MIT / Apache-2.0 / BSD；AGPL（Zotero 相关插件）与带附加条款的组件（如 MinerU）只做可选外部集成，不并入本代码。
