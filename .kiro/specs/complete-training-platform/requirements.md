# Requirements Document

## Introduction

raytrain 平台目前由三部分组成：`raytrain-server/`（FastAPI 控制面 + `/v1/console/*`
后端）、`raytrain-console/`（React + TypeScript + Tailwind 训练工作台前端，目前仅中文）、
`raytrain/`（CLI，含已经真实工作的 `RayLanceDataset`，通过 `RAYTRAIN_DATA_SOURCE_URI`
驱动 `ray.data.read_lance()` 读 MinIO）。集群侧已存在 KubeRay、Kueue、MinIO、Prometheus、
Loki。

问题在于：平台"看起来可用"，但多处链路其实只是 UI + 占位/种子数据，并未真正接到集群。
在真实集群部署联调中暴露出以下已确认的缺陷：

1. **开发机（Workspace）一创建就显示 "running"**——`create_workspace` 在 `create_pod`
   之后立刻把 DB state 写成 `running`，不查真实 Pod phase，即使 ImagePullBackOff 也显示运行中。
   底层确实是一个用 `RAYTRAIN_WORKSPACE_IMAGE` 起的 K8s Pod。
2. **无法 SSH / 打开 IDE 进入开发机**——`build_ide_urls` 只有在设置了
   `RAYTRAIN_WORKSPACE_BASE_DOMAIN` 时才返回链接，且没有任何 Ingress 把
   `ws-<id>.<domain>` 路由到 Pod 的 Jupyter(8888)/code-server(8080)/SSH(22)。
   接入 Ingress 从未部署，SSH 也没有 NodePort/网关。
3. **先停后启会失败**——`start_workspace` 重新创建 Pod，但被停止的 Pod 可能仍处于
   Terminating，导致 409 冲突；错误没有在 UI 友好提示。
4. **队列是硬编码的**——`queues_store.py` 的 `_DEFAULT_QUEUES` 种子是
   h20-research/h20-shared/a100-research/cpu-batch，与用户真实的 Kueue
   ClusterQueue/LocalQueue 不一致，且从不读取集群真实 Kueue 资源。
5. **训练"提交"只有在为该 gpu_type 配置了 `RAYTRAIN_SHARED_CLUSTERS` 时才真正运行**，
   否则只是平台记录。0→1 链路（构建/选择镜像 → 上传代码 → 提交 RayJob → worker 拉取
   working_dir → 通过 Ray Data 读 Lance 训练 → 日志/状态/产物）需要做到真实且可验证。
6. **Job Detail 的 Pods/Events/Metrics 标签页是派生/合成的**（只有 logs/status 是真实的）。
7. **jobs/queues/resources 存储无持久化**——它们是内存态，server 重启即丢失
   （users/workspaces/dev-sessions/datasets 在设置 `RAYTRAIN_DATABASE_URL` 时由 SQL
   存储持久化）。
8. **没有 HTTPS/Ingress**——前端通过 NodePort 暴露，JWT 走明文。

集群已安装 Prometheus + Loki。规划：训练日志 → Loki（任务结束后仍可查询），
GPU/内存/吞吐指标 → Prometheus，并预留未来用 agent 分析训练日志的扩展点。

本特性把这个"看起来可用"的平台变成**真实、端到端、完整可交付**的产品（非 MVP）。
用户已确认下列 8 个范围域全部纳入本次交付，外加一条横切要求："所有功能友好并且可用"
（无死按钮、无假状态、错误友好提示）。

设计与实现需尊重既有模式：双后端存储（内存 + SQL 在同一接口后，由 bootstrap 按
`RAYTRAIN_DATABASE_URL` 选择）；可注入客户端以便测试（`RayClusterClient`/`K8sClient`/
`SubmissionService` 均有注入缝）；测试使用 fake-Ray/内存模式，无需真实集群。

## Glossary

- **Platform / 平台**: raytrain 整体系统，含 raytrain-server 控制面、raytrain-console 前端、raytrain CLI。
- **Raytrain_Server**: `raytrain-server/` 的 FastAPI 控制面，持有唯一的 K8s 凭据，对外提供 `/v1/*` 与 `/v1/console/*`。
- **Console**: `raytrain-console/` 的 React 训练工作台前端。
- **Workspace（开发机）**: 用户长期使用的浏览器开发环境，对应一个 CPU K8s Pod（镜像由 `RAYTRAIN_WORKSPACE_IMAGE` 指定）+ 一个 RWX PVC（挂在 `/home/<user>`，跨 Pod 重启保留代码）+ 一个暴露 4 个 IDE 端口的 Service。源码见 `core/workspace.py`、`api/workspaces.py`。
- **DevSession**: 在 Workspace 基础上临时申请 GPU 的开发会话，对应一个带 GPU 的 Pod，有空闲与最长生命周期回收。源码见 `core/devsession.py`。
- **Pod_Phase**: K8s Pod 的真实相位（Pending / Running / Succeeded / Failed / Unknown / NotFound），由 `K8sClient.pod_phase` 读取。
- **IDE_Endpoint**: 开发机内的开发工具端口：Jupyter(8888)、code-server(8080)、PyCharm Projector(8887)、SSH(22)。
- **Access_Ingress**: 把外部请求路由到某个 Workspace/DevSession Pod 的 IDE_Endpoint 的接入机制（HTTP 经 Ingress 按 `ws-<id>.<domain>` 路由；SSH 经独立网关/NodePort）。当前缺失。
- **Workspace_Base_Domain**: 通配子域名根（如 `raytrain.example.com`），用于拼 IDE URL（`RAYTRAIN_WORKSPACE_BASE_DOMAIN`）。
- **RayJob**: KubeRay 自定义资源，描述一次提交到 RayCluster 的训练任务。
- **RayCluster**: KubeRay 自定义资源，一个长寿的 head + worker Pod 池。
- **Shared_Cluster**: 按 `gpu_type` 映射到 Ray Job Submission API 地址的长寿 RayCluster，来源于 `settings.shared_clusters`（`RAYTRAIN_SHARED_CLUSTERS`，JSON）。
- **Submission_Service**: console 任务记录与真实 Ray 提交之间的桥（`core/submission_service.py`），可注入 `RayClusterClient` 供测试。
- **Code_as_Submission（代码即提交）**: 把用户代码 zip 上传 MinIO 后，作为 RayJob `runtime_env.working_dir`，Ray 在每个 worker 上拉取解压；改代码后重新提交即生效，无需重建镜像。
- **Code_URI**: 上传后代码 zip 的对象地址，形如 `s3://raytrain-code/<user>/<job>.zip`，由 `PUT /v1/code` 返回。
- **Working_Dir**: RayJob `runtime_env.working_dir`，指向 Code_URI。
- **Lance**: 列式数据格式；训练侧通过 `ray.data.read_lance()` 从 MinIO 读取，由 `RAYTRAIN_DATA_SOURCE_URI` 指定来源。
- **Ray_Data**: Ray 的分布式数据加载层，`RayLanceDataset` 在其上读取 Lance。
- **Kueue**: K8s 批调度准入控制器。
- **ClusterQueue**: Kueue 集群级配额对象，定义某资源（如 nvidia.com/gpu）的名义配额与用量。
- **LocalQueue**: Kueue 命名空间级队列，指向一个 ClusterQueue，是用户提交任务时引用的队列。
- **Kueue_Reader**: 本特性新增组件，通过 K8s API 读取真实 ClusterQueue/LocalQueue 及其配额/用量，替换硬编码队列种子。
- **Loki**: 集群已装的日志聚合系统；训练日志写入后即使任务结束仍可按 label 查询。
- **Prometheus**: 集群已装的指标系统；提供 GPU 利用率、GPU 显存、吞吐等时序指标。
- **Loki_Client / Prometheus_Client**: 本特性新增的可注入客户端，分别查询 Loki/Prometheus，测试时以 fake 实现替换。
- **JobStore / QueueStore / ResourceStore**: 平台侧任务/队列/资源目录存储，当前为内存态（`jobs_store.py`/`queues_store.py`/`resources_store.py`）。
- **Sql_Backed_Store**: 由 `RAYTRAIN_DATABASE_URL` 在 bootstrap 选择的 SQL 存储实现，与内存实现共享接口（见 `core/sql_store.py`、`core/db.py`、`core/bootstrap.py`）。
- **Console_View**: 由 `console_views.py` 从存储记录派生的 Job Detail 子视图（timeline/pods/events/logs/metrics/artifacts）。
- **Live_Job**: 已真实提交到 Shared_Cluster 且持有 `submission_id` 的任务。
- **i18n_Locale**: 前端语言（`zh` 中文 / `en` 英文）。
- **Friendly_Error**: 面向用户的、可读的错误提示（含原因摘要与可行动建议），区别于裸异常 repr。

## Requirements

---

### 范围域 1 · 开发机（Workspace）完整生命周期

---

### Requirement 1: 开发机状态反映真实 Pod 相位（不再假 "running"）

**User Story:** 作为训练用户，我希望开发机列表与详情显示真实的 Pod 运行状态，
这样镜像拉取失败或 Pod 未就绪时我能立刻看出来，而不是被假的 "running" 误导。

#### Acceptance Criteria

1. WHEN 用户创建开发机且 `K8sClient.create_pod` 成功返回，THE Raytrain_Server SHALL 将该 Workspace 记录的 `state` 置为 `creating`，而不是直接置为 `running`。
2. WHEN 用户请求开发机列表或详情，THE Raytrain_Server SHALL 通过 `K8sClient.pod_phase` 读取该 Workspace Pod 的真实 Pod_Phase，并在响应的 `pod_phase` 字段返回该值。
3. WHILE Workspace Pod 的 Pod_Phase 为 `Running` 且其 IDE_Endpoint 健康检查通过，THE Raytrain_Server SHALL 将该 Workspace 的 `state` 报告为 `running`。
4. WHILE Workspace Pod 的 Pod_Phase 为 `Pending`，THE Raytrain_Server SHALL 将该 Workspace 的 `state` 报告为 `starting`。
5. IF Workspace Pod 处于 ImagePullBackOff、ErrImagePull、CrashLoopBackOff 或 Failed 等非就绪容器状态，THEN THE Raytrain_Server SHALL 将该 Workspace 的 `state` 报告为 `error`，并在响应中包含该容器状态的原因字符串。
6. IF `K8sClient.pod_phase` 返回 `NotFound`，THEN THE Raytrain_Server SHALL 将该 Workspace 的 `state` 报告为 `stopped`。
7. THE Console SHALL 在开发机页面展示 Raytrain_Server 返回的 `state` 与 `pod_phase`，且不得在前端把任何 Workspace 直接展示为 `running` 而未依据后端返回值。

### Requirement 2: 开发机镜像可配置

**User Story:** 作为用户，我希望创建开发机时可以选择或指定镜像，
这样不同项目能用不同的运行环境。

#### Acceptance Criteria

1. WHEN 用户创建开发机且请求体提供了 `image`，THE Raytrain_Server SHALL 使用该 `image` 作为 Workspace Pod 的容器镜像。
2. WHEN 用户创建开发机且未提供 `image`，THE Raytrain_Server SHALL 使用 `settings.workspace_image`（`RAYTRAIN_WORKSPACE_IMAGE`）作为默认镜像。
3. THE Console SHALL 在创建开发机表单中提供镜像选择项，候选来源为 Admin 维护的 Runtime_Image 资源目录，并允许用户填写自定义镜像地址。
4. WHERE 用户提交了自定义镜像地址，THE Raytrain_Server SHALL 校验该地址为非空且符合容器镜像引用格式，校验失败时返回 400 与 Friendly_Error。

### Requirement 3: 开发机 SSH 与 IDE 接入真实可用

**User Story:** 作为用户，我希望从浏览器打开开发机的 Jupyter / VS Code，
并能用 SSH 连入，这样我无需 kubectl 端口转发就能在开发机里写代码。

#### Acceptance Criteria

1. THE Platform SHALL 部署 Access_Ingress，将主机名 `ws-<workspace_id>.<Workspace_Base_Domain>` 的 HTTP(S) 请求按路径路由到对应 Workspace Pod 的 Jupyter(8888) 与 code-server(8080) 端口。
2. WHEN 某 Workspace 的 Pod_Phase 为 `Running` 且设置了 Workspace_Base_Domain，THE Raytrain_Server SHALL 在该 Workspace 详情的 `ide_urls` 中返回可访问的 `jupyter` 与 `code` HTTPS URL。
3. THE Platform SHALL 为 Workspace Pod 的 SSH(22) 端口提供外部接入通道（SSH 网关或 NodePort），并在 `ide_urls.ssh` 返回用户可直接使用的 `ssh://host:port` 连接串。
4. WHEN 用户在 Console 点击某 IDE_Endpoint 链接且该 Workspace 状态为 `running`，THE Console SHALL 在新标签页打开对应的可访问 URL。
5. IF Workspace_Base_Domain 未配置，THEN THE Raytrain_Server SHALL 返回空的 HTTP `ide_urls`，且 THE Console SHALL 显示"接入域名未配置"的 Friendly_Error，而不是渲染无法点击的死链接。
6. WHILE 某 Workspace 的 `state` 不为 `running`，THE Console SHALL 将该 Workspace 的 IDE_Endpoint 入口置为不可点击，并提示当前状态。
7. WHEN Access_Ingress 收到指向不存在或非 `running` Workspace 的请求，THE Access_Ingress SHALL 返回 HTTP 404 或 503，而不是连接挂起。

### Requirement 4: 开发机停止/启动可靠（处理 Terminating/409）

**User Story:** 作为用户，我希望停止开发机后还能再次启动它，
这样我能在不用时释放资源、需要时再恢复，而不会卡在冲突错误上。

#### Acceptance Criteria

1. WHEN 用户停止开发机，THE Raytrain_Server SHALL 删除该 Workspace Pod、保留其 PVC，并将 `state` 置为 `stopping`。
2. WHILE 被停止的 Workspace Pod 仍存在于集群（处于 Terminating），THE Raytrain_Server SHALL 将该 Workspace 的 `state` 报告为 `stopping`。
3. WHEN 用户启动一个先前停止的开发机且其旧 Pod 仍处于 Terminating，THE Raytrain_Server SHALL 等待旧 Pod 完全删除后再创建新 Pod，等待上限为可配置的超时（默认 60 秒）。
4. IF 创建 Workspace Pod 时 K8s 返回 409 冲突，THEN THE Raytrain_Server SHALL 不向用户抛出裸 409，而是返回带 Friendly_Error 的 409 响应，说明上一个 Pod 仍在终止、请稍后重试。
5. IF 启动等待超过超时上限旧 Pod 仍未删除，THEN THE Raytrain_Server SHALL 返回 409 与 Friendly_Error，提示用户当前无法启动及原因。
6. WHEN 启动成功创建新 Pod，THE Raytrain_Server SHALL 将 `state` 置为 `creating`，并由 Requirement 1 的相位映射后续转为 `running`。
7. WHEN 开发机停止与启动操作完成，THE Raytrain_Server SHALL 把该操作写入审计日志（含 user、action、resource、result）。

---

### 范围域 2 · 训练 0→1 真实运行（含代码即提交验证）

---

### Requirement 5: 通过 Console 提交真实 RayJob

**User Story:** 作为用户，我希望在 Console 的创建任务向导里选镜像、传代码、提交，
任务能真实跑在 Ray 集群上，这样我不必手写 kubectl/ray job submit。

#### Acceptance Criteria

1. WHEN 用户提交任务且该任务 `gpu_type` 在 `settings.shared_clusters` 中存在地址，THE Submission_Service SHALL 通过 `RayClusterClient.submit_job` 向对应 Shared_Cluster 提交一个 RayJob，并把返回的 `submission_id` 与集群地址写入该任务记录。
2. WHEN Submission_Service 成功提交 RayJob，THE Raytrain_Server SHALL 把该任务 `status` 置为 `Starting` 并标记为 Live_Job。
3. WHEN 提交任务时选择了某数据集，THE Submission_Service SHALL 把该数据集 URI 作为 `RAYTRAIN_DATA_SOURCE_URI` 注入 RayJob 的 `runtime_env.env_vars`，使训练侧可经 Ray_Data 读取 Lance。
4. IF 任务 `gpu_type` 未在 `settings.shared_clusters` 中配置，THEN THE Raytrain_Server SHALL 拒绝该提交并返回 Friendly_Error，说明该 gpu_type 无可用集群，而不是静默创建一条永远 Queued 的占位记录。
5. IF `RayClusterClient.submit_job` 抛出异常，THEN THE Submission_Service SHALL 把任务 `status` 置为 `Failed`，记录 `FailureInfo`（category=`SubmitError`、含错误摘要与详情），且不使请求崩溃。
6. WHEN 用户在 Console 取消一个 Live_Job，THE Submission_Service SHALL 调用 `RayClusterClient.stop` 停止真实 RayJob，并将任务 `status` 置为 `Cancelled`。
7. THE Raytrain_Server SHALL 周期性或在列表/详情请求时通过 `RayClusterClient.get_status` 协调 Live_Job 的状态，把 Ray 状态映射为 console 状态（PENDING→Queued、RUNNING→Running、SUCCEEDED→Succeeded、FAILED→Failed、STOPPED→Cancelled）。

### Requirement 6: 代码即提交（改代码 → 重新提交 → 无需重建镜像）

**User Story:** 作为用户，我希望改完训练代码后重新提交就能用上新代码，
而不必每次重建并推送镜像，这样迭代更快。

#### Acceptance Criteria

1. WHEN 用户上传代码 zip 至 `PUT /v1/code`，THE Raytrain_Server SHALL 把 zip 存入 MinIO 的 `code_bucket`，并返回形如 `s3://<bucket>/<user>/<job>.zip` 的 Code_URI 与 SHA256。
2. WHEN 提交任务时提供了 `code_uri`，THE Submission_Service SHALL 把该 Code_URI 设为 RayJob `runtime_env.working_dir`，使每个 worker 拉取并解压该代码。
3. WHEN 同一任务在代码内容变化后以新的 Code_URI 重新提交，THE Submission_Service SHALL 使用新的 Code_URI 提交新的 RayJob，且不要求变更或重建容器镜像。
4. THE Raytrain_Server SHALL 在任务记录中保存所用的 Code_URI，并在 Job Detail 中展示。
5. IF 上传的 zip 体积超过服务端上限或 SHA256 校验不一致，THEN THE Raytrain_Server SHALL 返回 4xx 与 Friendly_Error，并不存储该对象。

### Requirement 7: 端到端 0→1 路径可验证

**User Story:** 作为平台运维，我希望能在不依赖真实 GPU 的情况下验证整条提交链路，
这样我能在 CI 中回归 0→1 流程的正确性。

#### Acceptance Criteria

1. THE Raytrain_Server SHALL 通过注入的 `RayClusterClient` 完成提交链路，使测试可用 fake-Ray 实现验证：上传代码 → 构造 `runtime_env`（含 working_dir 与 `RAYTRAIN_DATA_SOURCE_URI`）→ 提交 → 状态协调，全程无需真实集群。
2. WHEN 使用 fake-Ray 提交一个任务，THE 提交链路 SHALL 在传给 Ray 的 `runtime_env.working_dir` 中包含该任务的 Code_URI。
3. WHEN 使用 fake-Ray 提交一个含数据集的任务，THE 提交链路 SHALL 在传给 Ray 的 `runtime_env.env_vars` 中包含正确的 `RAYTRAIN_DATA_SOURCE_URI`。
4. THE Platform SHALL 提供一份端到端 runbook，描述在配置了 Shared_Cluster 的真实集群上执行 0→1（构建/选镜像 → 传代码 → 提交 → worker 拉取 working_dir → 经 Ray_Data 读 Lance → 出日志/状态/产物）的验收步骤。

---

### 范围域 3 · 训练日志接入 Loki

---

### Requirement 8: Job Detail 通过 Loki 查看训练日志（运行中与结束后）

**User Story:** 作为用户，我希望在任务运行中和结束后都能在 Job Detail 看到训练日志，
这样任务跑完后我仍能排查问题，而不是日志随任务消失。

#### Acceptance Criteria

1. WHILE 某 Live_Job 正在运行，WHEN 用户打开其 Job Detail 日志视图，THE Raytrain_Server SHALL 通过 Loki_Client 按该任务的 label（如 `submission_id`/job 名）查询并返回日志行。
2. WHEN 某 Live_Job 已结束（Succeeded/Failed/Cancelled），WHEN 用户打开其 Job Detail 日志视图，THE Raytrain_Server SHALL 仍能通过 Loki_Client 查询并返回该任务在运行期间产生的日志。
3. THE Raytrain_Server SHALL 在 Loki 查询中支持按时间范围与按容器/Pod 过滤，并在响应中标注每行所属容器与时间戳。
4. IF Loki 查询失败或超时，THEN THE Raytrain_Server SHALL 返回 Friendly_Error，提示日志暂不可用及原因，且不使 Job Detail 请求崩溃。
5. WHEN 日志结果超过单次返回上限，THE Raytrain_Server SHALL 分页或限量返回，并提供继续获取更早/更晚日志的游标。
6. THE Console SHALL 在 Job Detail 日志标签页展示 Loki 返回的真实日志，并标明数据来源为 Loki（非合成数据）。

---

### 范围域 4 · 队列读取真实 Kueue 资源

---

### Requirement 9: 队列来自集群真实 Kueue 资源（非硬编码）

**User Story:** 作为用户，我希望 Console 的队列页面显示集群里真实存在的队列及其配额和用量，
这样我看到的队列就是我提交任务时能用的队列。

#### Acceptance Criteria

1. WHEN 用户请求队列列表，THE Kueue_Reader SHALL 通过 K8s API 读取集群真实的 ClusterQueue 与 LocalQueue，并据此返回队列列表，而不是返回 `_DEFAULT_QUEUES` 硬编码种子。
2. THE Kueue_Reader SHALL 为每个队列返回其名义配额（nominal quota）、已用量（used）、准入数（admitted）与等待数（pending），数据来源为对应 ClusterQueue/LocalQueue 的 status。
3. THE Kueue_Reader SHALL 把每个 LocalQueue 关联到其所引用的 ClusterQueue，并在返回中体现该关联（如 `cluster_queue` 字段）。
4. IF 读取 Kueue 资源失败（如 CRD 不存在或权限不足），THEN THE Raytrain_Server SHALL 返回 Friendly_Error 说明无法读取队列及原因，且不回退到误导性的硬编码队列。
5. WHEN Console 创建任务向导加载队列候选项，THE Console SHALL 仅展示 Kueue_Reader 返回的真实队列。
6. WHERE 某 gpu_type 没有对应的真实队列，THE Console SHALL 阻止用户为该 gpu_type 提交任务，并提示无可用队列。
7. THE Raytrain_Server SHALL 为 Kueue_Reader 提供注入缝，使测试可用 fake Kueue 数据验证队列读取与映射逻辑，无需真实集群。

---

### 范围域 5 · 指标接入 Prometheus

---

### Requirement 10: Job Detail Metrics 标签页展示 Prometheus 真实指标

**User Story:** 作为用户，我希望在 Job Detail 的 Metrics 标签页看到该任务真实的 GPU 利用率、
GPU 显存与吞吐曲线，这样我能判断训练是否健康，而不是看合成数据。

#### Acceptance Criteria

1. WHEN 用户打开某 Live_Job 的 Metrics 标签页，THE Raytrain_Server SHALL 通过 Prometheus_Client 查询该任务对应 Pod 的 GPU 利用率、GPU 显存用量与训练吞吐，并返回为时序数据。
2. THE Prometheus_Client SHALL 用该任务的标识（如 RayJob/Pod label）约束 Prometheus 查询，使返回的指标仅属于该任务。
3. WHEN 某指标在 Prometheus 中无数据（如任务尚未产生该指标），THE Raytrain_Server SHALL 对该指标返回空序列，并在响应中标注该指标当前无数据。
4. IF Prometheus 查询失败或超时，THEN THE Raytrain_Server SHALL 返回 Friendly_Error 说明指标暂不可用及原因，且不使 Metrics 请求崩溃。
5. THE Console SHALL 在 Metrics 标签页渲染 Prometheus 返回的真实时序，并标明数据来源为 Prometheus（非合成数据）。
6. THE Raytrain_Server SHALL 为 Prometheus_Client 提供注入缝，使测试可用 fake 指标数据验证查询与映射逻辑，无需真实集群。

---

### 范围域 6 · 持久化（jobs/queues/resources 存储重启不丢）

---

### Requirement 11: jobs/queues/resources 存储 SQL 持久化

**User Story:** 作为平台运维，我希望任务、队列、资源目录在 server 重启后仍然存在，
这样一次重启不会让用户的任务历史和管理配置全部消失。

#### Acceptance Criteria

1. WHILE `settings.database_url` 非空，THE Raytrain_Server SHALL 在 bootstrap 阶段为 JobStore、QueueStore、ResourceStore 选择 Sql_Backed_Store 实现，与现有 users/workspaces/dev-sessions/datasets 一致。
2. THE Sql_Backed_Store SHALL 与各自的内存实现共享相同的方法签名，使 API 层无需感知后端差异。
3. THE Raytrain_Server SHALL 在 `db.py` 的 `init_schema` 中为 jobs、queues、resources 创建对应表（缺失时创建），并在 sqlite 与 postgres 两种后端均可用。
4. WHEN server 写入或更新一条任务/队列/资源记录后重启（`database_url` 已配置），THE Raytrain_Server SHALL 在重启后仍能读取到该记录。
5. WHILE `settings.database_url` 为空，THE Raytrain_Server SHALL 继续使用内存存储（开发/测试），且行为与持久化模式在接口层一致。
6. WHERE QueueStore 的队列数据来源为 Kueue_Reader（Requirement 9）而非平台自管，THE Raytrain_Server SHALL 持久化仅属于平台自管的队列元数据（如展示别名/排序），不持久化从 Kueue 实时读取的用量字段。

---

### 范围域 7 · 前端国际化（中/英切换）

---

### Requirement 12: Console 全站中英文切换

**User Story:** 作为用户，我希望能在中文和英文之间切换整个 Console 界面，
这样中英文团队成员都能顺畅使用平台。

#### Acceptance Criteria

1. THE Console SHALL 提供语言切换控件，支持在 `zh`（中文）与 `en`（英文）之间切换 i18n_Locale。
2. WHEN 用户切换 i18n_Locale，THE Console SHALL 立即以所选语言重新渲染所有页面（Overview、开发机/Workspaces、Create Job、Training Jobs、Job Detail 各标签页、Queues、Experiments、Artifacts、Datasets、Admin、Login）的界面文案。
3. THE Console SHALL 把用户所选 i18n_Locale 持久化到浏览器本地存储，并在下次加载时沿用。
4. WHERE 某界面文案在当前 i18n_Locale 下缺失翻译，THE Console SHALL 回退展示中文文案，而不是展示原始翻译键名或空白。
5. THE Console SHALL 在所选 i18n_Locale 下本地化日期、时间与数字的展示格式。
6. WHEN 后端返回 Friendly_Error，THE Console SHALL 按当前 i18n_Locale 展示对应语言的提示文案。

---

### 范围域 8 · 接入与安全（Ingress + HTTPS）

---

### Requirement 13: Web 控制台与 API 经 Ingress + HTTPS 暴露

**User Story:** 作为平台运维，我希望 Console 和 API 通过带 HTTPS 的 Ingress 对外提供，
这样登录凭据和 JWT 不再以明文在网络上传输。

#### Acceptance Criteria

1. THE Platform SHALL 通过 Ingress 暴露 Console 与 Raytrain_Server `/v1` API，替代当前的 NodePort 直连。
2. THE Platform SHALL 为该 Ingress 配置 TLS 证书，使 Console 与 API 经 HTTPS 提供服务。
3. WHEN 客户端经 HTTP 访问该 Ingress，THE Ingress SHALL 将请求重定向到 HTTPS。
4. THE Platform SHALL 在部署文档中明确：当前认证为用户名/密码 + JWT，且在引入 HTTPS 之前 JWT 经明文传输，属于已知安全风险。
5. THE Raytrain_Server SHALL 将 CORS 允许来源配置为 Console 的实际 HTTPS 源，而非生产环境下的通配 `*`。
6. WHERE TLS 证书或 Ingress 控制器未就绪，THE Platform 部署文档 SHALL 提供可操作的回退/排查指引。

---

### 横切要求 · 所有功能友好且可用

---

### Requirement 14: 每个可见前端功能都真实可用、错误友好

**User Story:** 作为用户，我希望 Console 上每个能看到的按钮和链接都真实生效，
失败时给我可读的提示，这样我不会点到死按钮或被假状态误导（"所有功能友好并且可用"）。

#### Acceptance Criteria

1. THE Console SHALL 使每个可见的操作控件（按钮/链接/表单提交）触发真实的后端调用或受支持的客户端行为，不得存在无任何效果的死控件。
2. WHEN 任一后端调用返回错误，THE Console SHALL 展示 Friendly_Error（含原因摘要），不得仅打印裸异常或静默失败。
3. WHILE 某项操作正在进行，THE Console SHALL 显示进行中状态（如禁用按钮或加载指示），以避免用户重复触发。
4. WHERE 某功能依赖的后端能力未配置（如接入域名缺失、无可用集群、Loki/Prometheus 不可达），THE Console SHALL 以 Friendly_Error 说明该前置条件，而不是呈现可点击但必然失败的入口。
5. THE Console SHALL 移除或以真实数据替换所有占位/合成展示，使页面呈现的状态来自后端真实数据；任何尚不可用的真实数据 SHALL 显式标注为不可用，而非用合成值伪装。
6. WHEN 某列表当前没有真实数据，THE Console SHALL 展示明确的空状态提示，而不是展示种子/演示数据。
7. THE Raytrain_Server SHALL 对面向用户的错误响应统一返回结构化的 Friendly_Error 负载（含可读 message 与可选原因码），供 Console 本地化展示。
