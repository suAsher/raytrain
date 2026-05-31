# raytrain-console

面向算法工程师和平台管理员的**训练任务工作台**（KubeRay + Kueue），高保真前端原型，
带完整 mock 数据。定位是训练工作台，不是 Kubernetes 资源控制台——用户不写 RayJob
YAML，也看不到过多 K8s 细节。

## 技术栈

- React 18 + TypeScript + Vite
- React Router（路由）
- Tailwind CSS（中性色工作台主题，圆角 ≤ 8px）
- lucide-react（图标）
- Recharts（指标图表）
- 轻量自写 UI 原语（StatusBadge / Panel / Tabs / QuotaUsageBar…），无重组件库依赖

## 运行

```bash
cd raytrain-console
npm install
npm run dev      # http://localhost:5174
npm run build    # 类型检查 + 产物
```

全部数据来自 `src/lib/mockData.ts` + `src/lib/mockGen.ts`，无需后端即可点通所有流程。

## 信息架构

左侧导航：Overview / Training Jobs / Create Job / Queues / Experiments /
Artifacts / Datasets / Admin。
顶部栏：Tenant/Project 切换、QuotaGroup、GPU/CPU/MEM 配额摘要、全局搜索、用户菜单。

## 页面 / 能力

- **Overview**：Running/Queued/Failed/Succeeded 计数、项目配额、资源池（H20/A100/CPU）、
  最近失败（突出失败原因）+ 最近运行。
- **Training Jobs**：高密度表格 + 多维筛选（状态/队列/GPU/创建人/只看我的/仅失败）+
  行操作（View/Logs/Cancel/Retry/Clone/Open Ray Dashboard）。
- **Create Job**：5 步向导（基础信息 → 代码与镜像 → 资源 → 数据与 checkpoint →
  Review）。右栏实时资源估算（总 GPU/CPU/内存、预计排队）；config 下拉自动拼命令；
  多节点 checkpoint 非共享时**阻断提交**；Review 折叠 dry-run YAML，校验错误清晰展示。
  提交后真正写入 store 并跳转到 Job Detail。
- **Job Detail**：摘要区 + 失败横幅（一眼看原因 + 跳日志/事件/Retry）+ 7 个 Tab：
  Overview（状态时间线/资源/数据摘要）、Logs（终端式、按容器切换、follow/搜索/下载、
  失败自动定位错误）、Events（K8s 事件翻译成可读原因）、Pods（head/worker/submitter
  表格，点开看详情）、Metrics（GPU 利用率/显存/CPU/内存/object store/吞吐）、
  Config（用户配置 + 折叠的渲染后 RayJob manifest）、Artifacts（checkpoint/model/log/eval）。
- **Queues**：Kueue 视角（nominal/used/admitted/pending/wait time/健康度/最近任务），
  不暴露 CRD。
- **Experiments**：实验分组 + Clone/Retry 复现。
- **Artifacts**：全局产物检索（按 kind/job 筛选）。
- **Datasets**：数据挂载注册表（可见性/行数/大小/挂载路径）。
- **Admin**：Projects/QuotaGroups/Users/ResourceProfiles/Queues/Images/ResourceFlavors，
  第一版只读，已预留 New/Edit 入口。

## 交互原则落地

- 提交前必看资源估算 + 校验结果。
- 失败状态不是只显示 "Failed"，而是给出分类（OOMKilled / ImagePullBackOff…）+ 可读原因
  + 修复建议 + 一键跳日志/事件/Retry。
- RayJob / Pod / PVC / Kueue Workload 不作为主概念，YAML 默认折叠在 Advanced 区。
- Clone 复用配置、Retry 保留原配置生成新 run（Queued）。
- 日志 / 事件 / 指标可相互跳转。

## 接后端

登录鉴权、Datasets、Create Job 提交已对接真实 `raytrain-server`；其余页面
（Queues/Experiments/Artifacts/Job 详情指标）后端 API 待补，暂用演示数据，登录后顶部
会显示「演示数据」提示条。

- 鉴权：登录页粘贴管理员签发的 token → `/v1/auth/me` 校验 → 401 自动回登录页。
- 本地联调：`raytrain-server` 跑在 `:8099`，`vite.config.ts` 已把 `/v1` 代理过去；
  生产由 `nginx.conf` 反代到 `raytrain-server` 的 ClusterIP。
- 继续接入：把各页面的 mock 调用换成 `apiFetch(...)`（见 `src/lib/api.ts`，已封装
  token 注入 + 401 处理 + `withFallback` 演示降级）。类型已在 `src/lib/types.ts` 对齐。

## 部署

已纳入平台部署链路（`raytrain-server/deploy/kustomization.yaml` 引用
`raytrain-console/deploy/web.yaml`）。构建镜像后 `kubectl apply -k raytrain-server/deploy/`
一并拉起。详见 `docs/platform-deploy.md` §4.2。
