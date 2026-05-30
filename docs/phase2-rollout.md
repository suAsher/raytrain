# Phase 2 · 共享集群灰度推开计划与数据采集模板

对应 spec 任务 `long-term-evolution / 10.3`：在 **5 名白名单用户**上灰度 1–2 周
（`default_cluster_mode=shared`），每天检查 **submission 成功率、worker 启动时延、
autoscaling 行为、log 流稳定性**，把数据汇总到本文，并据此判定是否扩大白名单 / 全量
切换（任务 10.4）。前置依赖：任务 **8.4**（CLI 适配 cluster_mode / shared 提交链路）、
任务 **9.2**（token 颁发与多租户鉴权）。

> ⚠️ 重要说明（deferred）：本文是**灰度执行计划 + 数据采集模板**。实际的「1–2 周
> 灰度数据收集」需要一套带 KubeRay + GPU 的活跃长寿集群、部署好的 Submission_Server、
> 5 名真实白名单用户、以及跨 1–2 周的连续观测，本地环境无法完成。因此**实机灰度被
> deferred 给运维在真实金丝雀（canary）窗口执行**。运维按本文的命令采集数据、把结果
> 填进下面的空表格，并据「判定与下一步」一节决定扩大白名单 / 全量切换或回滚。

本文与下列文档共享术语与命令，请保持一致：

- `docs/migration-shared-cluster.md`（任务 10.2 · 迁移步骤与**回退方法 §3**，本文回滚
  预案直接引用它）。
- `docs/phase1-rollout.md`（Phase 1 灰度模板，本文沿用其结构与 deferred 说明风格）。
- `deploy/shared-cluster/README.md`（长寿集群部署 / 升级 drain / 排障，含 autoscaler、
  head Service `8265`、`ray.io/cluster=ray-shared-h20` 标签）。
- `deploy/set-default-cluster-mode.sh`（灰度开关：写 namespace ConfigMap
  `raytrain-defaults`）。
- `deploy/server/deployment.yaml`（Submission_Server，namespace `raytrain-system`，
  Deployment `raytrain-server`）。

---

## 1. 灰度目标与范围

### 目标

验证 Phase 2（shared 模式：CLI → Submission_Server → Ray Job Submission API → 长寿
RayCluster）在真实用户、真实任务体量下稳定可用，把它从「迁移步骤跑通」推进到「可扩大
白名单 / 全量默认开启」（任务 10.4）。

### 范围

| 项 | 值 |
| --- | --- |
| 灰度用户 | **5 名白名单用户**（已由任务 9.2 / `deploy/issue-token.sh` 颁发 token） |
| 默认模式 | **shared**（在白名单用户所在 namespace 写 `default_cluster_mode=shared`） |
| 长寿集群 | `ray-shared` namespace 下的 `ray-shared-h20`（及按需 `ray-shared-a100`） |
| 观察窗口 | **连续 1–2 周**（逐日记录，建议至少 7 天，最多 14 天） |
| 回退手段 | 始终保留 `--cluster-mode per_job` / namespace 切回 per_job（详见 §6 回滚预案） |

灰度开始前确认前置依赖已就绪（任务 8.4 / 9.2，迁移步骤见
`docs/migration-shared-cluster.md` §1）：

```bash
# 1) 长寿集群 head 已 Running（worker 此时为 0，正常）
kubectl -n ray-shared get raycluster -l raytrain.shared=true
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20

# 2) Submission_Server 已就绪（ClusterIP:8080，replicas 全 Ready）
kubectl -n raytrain-system rollout status deploy/raytrain-server
kubectl -n raytrain-system get svc raytrain-server

# 3) 集群内自测 /healthz
kubectl -n raytrain-system run curl --rm -it --image=curlimages/curl --restart=Never -- \
    curl -fsS http://raytrain-server.raytrain-system.svc:8080/healthz   # 期望 {"status":"ok"}
```

### 切换灰度开关（把白名单用户所在 namespace 默认模式切到 shared）

`set-default-cluster-mode.sh` 写 namespace ConfigMap `raytrain-defaults`，是 cluster-mode
解析的**中间层**（优先级：CLI flag `--cluster-mode` > 本 ConfigMap > 用户配置 >
兜底 per_job）。对白名单用户所在的每个 namespace 执行：

```bash
# 把白名单用户所在 namespace 的默认模式切到 shared（按实际 namespace 替换）
deploy/set-default-cluster-mode.sh shared --namespace <ns>

# 验证（应输出 shared）
kubectl -n <ns> get configmap raytrain-defaults \
    -o jsonpath='{.data.default_cluster_mode}'
```

### 成功判定标准（1–2 周窗口结束时综合评估）

| 指标 | 阈值（满足即视为通过） |
| --- | --- |
| submission 成功率 | **≥ 95%**（`成功提交数 / 总提交尝试`，窗口累计），且无持续下滑 |
| 单日 submission 成功率 | 任意单日 **≥ 90%**，无连续两天低于阈值 |
| worker 启动时延 | 中位 **≤ 90s**、p95 **≤ 180s**（从 submit 到首个 worker `Running`） |
| autoscaling 行为 | worker 能正常 `0 → N → 0`，**无卡住扩不起来 / 缩不回去**的异常 |
| log 流稳定性 | `raytrain logs -f` 单次连接**无频繁断连**（窗口内异常断连 ≤ 个位数且可重连） |

任一硬指标不达标，进入 §6 回滚预案；全部达标则推进任务 **10.4**（扩大白名单 → 全量
切换）。

---

## 2. 监控指标与采集方法

四个核心指标：**submission 成功率**、**worker 启动时延**、**autoscaling 行为**、
**log 流稳定性**。下面给出可直接复制粘贴的命令。namespace 统一用占位 `<ns>`（灰度
namespace）、`ray-shared`（长寿集群）、`raytrain-system`（server）；GPU 池以 `h20` /
`ray-shared-h20` 为例，A100 池把 `h20` 换成 `a100` 即可。

### 2.1 submission 成功率

定义：

```text
submission 成功率 = 成功提交数 / 总提交尝试（同一天 / 整个窗口）
```

**来源 A · server audit log（最权威，覆盖所有经 server 的提交）**

server 在每次提交后写结构化审计日志：logger 名 **`raytrain.audit`**（INFO 级），消息以
`job_submit ` 开头，后接 JSON。JSON 里的 `outcome` 字段区分结果：

- `outcome="submitted"` —— 提交成功（带 `submission_id`）。
- `outcome="error"` —— 提交失败（带 `code` / `message`，如 `unknown_gpu_type` 或上游
  重试耗尽）。
- `outcome="stopped"` —— 是 `stop` 操作的审计（`action="stop"`），**不计入提交尝试**。

server Deployment 是 `raytrain-server`（namespace `raytrain-system`，多副本），用
`deploy/...` 选择器一次抓全部副本：

```bash
# 全部审计行（提交 + 停止），按需加 --since=24h / --timestamps
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h | grep job_submit

# 当天「成功提交数」= outcome=submitted 的行数
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h \
    | grep job_submit | grep -c '"outcome": "submitted"'

# 当天「失败提交数」= outcome=error 的行数
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h \
    | grep job_submit | grep -c '"outcome": "error"'
```

> 总提交尝试 = `submitted` + `error`（排除 `outcome="stopped"`，那是 stop 操作）。
> 成功率 = `submitted / (submitted + error)`。多副本时 `deploy/raytrain-server`
> 选择器会聚合所有 pod 的日志；若 server 被重启 / 滚动过，注意用 `--since` 圈定窗口，
> 必要时把每日日志归档后离线统计，避免日志轮转丢数。

**来源 B · 客户端 exit code（交叉校验，用户侧）**

`raytrain submit` 提交失败时以非 0 退出码结束（shared 模式下 `PlatformError` 经
`click.ClickException` 抛出）。请白名单用户在反馈时附上失败任务的命令与退出码：

```bash
raytrain submit --config <config> --gpus 8 --nodes 1 --gpu-type h20 --name <exp>
echo "exit=$?"   # 0 = 提交成功；非 0 = 提交失败（附报错给运维）
```

把两来源对齐后按天填入 §3 每日指标表的「提交尝试 / 成功 / 成功率」列。

### 2.2 worker 启动时延

定义：**从提交时刻（submit ts）到该任务在长寿集群里拉起的首个 worker pod 进入
`Running` 的时刻（worker Running ts）之间的耗时**。shared 模式下提交不创建 per-job
RayCluster，而是把任务投递到长寿集群，由 autoscaler 把 worker 从 0 扩起。

**(1) submit ts**：取 §2.1 audit log 中该提交 `outcome="submitted"` 行的 `ts` 字段
（server 写的是 UTC ISO8601），或客户端提交成功的时间戳。

**(2) worker Running ts**：观察长寿集群 worker pod 何时进入 `Running`：

```bash
# a) 实时 watch worker pod 状态变化（带本地时间戳），从 Pending → Running
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20 -w

# b) 用 events 看 worker pod 的 Scheduled / Started 时间点（按时间排序）
kubectl -n ray-shared get events --sort-by='.lastTimestamp' \
    | grep -Ei 'ray-shared-h20.*(Scheduled|Pulling|Pulled|Created|Started)'

# c) 直接读某个 worker pod 的状态时间戳（startTime / containerStatuses.startedAt）
kubectl -n ray-shared get pod <worker-pod> \
    -o jsonpath='{.metadata.creationTimestamp}{"  "}{.status.startTime}{"\n"}'
```

**(3) Ray dashboard 交叉验证**（8265 = dashboard + Job Submission API）：

```bash
kubectl -n ray-shared port-forward svc/ray-shared-h20-head 8265:8265
# 浏览器开 http://localhost:8265 看 Cluster / Jobs 页的 worker 起停时间
```

启动时延 = `worker Running ts − submit ts`。每天取多次提交的**中位数**与 **p95**，
填入 §3 每日指标表。

### 2.3 autoscaling 行为

目标：观察 worker 副本能否随任务正常 `0 → N → 0`（空闲缩 0、有任务按需扩，最多
`maxReplicas: 16`，每 worker 1 GPU），且**无卡住**（扩不起来 / 缩不回去）。

```bash
# a) RayCluster 状态（期望/就绪 worker 数、autoscaler 状态）
kubectl -n ray-shared get raycluster ray-shared-h20 -o yaml | \
    grep -A30 'status:'

# b) autoscaler 事件（扩缩决策、扩不起来的原因都在这里）
kubectl -n ray-shared describe raycluster ray-shared-h20

# c) 按时间观察 worker pod 数量变化（投递任务后看 0→N，任务结束后看 N→0）
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20 -w

# d) 某一时刻的 worker 计数快照
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20 --no-headers | wc -l
```

判异常（计入 §3「autoscaling 异常次数」）：worker 长时间 `Pending` 扩不起来、任务结束
后 worker 迟迟不缩回 0、撞 `maxReplicas=16` 之外的非预期排队等。常见原因与排查
（节点缺 `gpu` 标签、GPU 耗尽、operator 异常）见
`deploy/shared-cluster/README.md` 的「worker 扩不起来」一节。

### 2.4 log 流稳定性

目标：`raytrain logs <submission_id> -f --gpu-type h20` 在跟随期间**保持连接，不频繁
断连 / 卡死**。shared 模式日志走 server 的 SSE（`text/event-stream`），server 把每个
上游 chunk 以 `data: ...` 帧下发；上游中断时 server 会发一帧
`event: error` + `data: log stream interrupted` 后**优雅结束**（不会 500，也不会
吊住连接）。

```bash
# 跟随某个 submission 的日志（shared 模式建议显式带 --gpu-type）
raytrain logs <submission_id> -f --gpu-type h20
```

每次跟随时记录：是否中途断连、是否出现 `log stream interrupted`（SSE error 帧）、断连
后重连是否恢复。统计窗口内的断连 / SSE error 次数，填入 §3「log 流断连次数」。如需在
server 侧交叉确认是上游（Ray dashboard / 任务结束）还是网络导致的中断：

```bash
# server 侧看 logs 端点相关报错 / 上游中断
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h \
    | grep -Ei 'logs|stream|interrupted'
```

---

## 3. 数据记录表格（灰度期间填写）

> 以下为空模板，运维在真实金丝雀窗口逐日填入。namespace、日期、用户按实际替换。

### 3.0 灰度元信息

| 项 | 值 |
| --- | --- |
| 灰度 namespace(s) | _（待填，如 team-a / team-b）_ |
| 白名单用户（5 名） | _（待填：user × 5）_ |
| 长寿集群 / GPU 池 | _（待填，如 ray-shared-h20 [+ ray-shared-a100]）_ |
| 灰度起止 | _（待填：起 ~ 止，1–2 周）_ |
| raytrain 版本 / commit | _（待填）_ |
| server 镜像 tag | _（待填，raytrain-server tag）_ |
| env-only 集群镜像 tag | _（待填）_ |
| 负责人 | _（待填）_ |

### 3.1 每日指标表

| 日期 | 提交尝试 | 成功 | 成功率 | 中位 worker 启动时延 | p95 时延 | autoscaling 异常次数 | log 流断连次数 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Day 1 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| Day 2 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| Day 3 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| Day 4 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| Day 5 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| Day 6 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| Day 7 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |  |
| _（按需续行至 Day 14）_ |  |  |  |  |  |  |  |  |
| **窗口累计** |  |  |  |  |  |  |  |  |

### 3.2 用户反馈表

严重度分 高 / 中 / 低（同 phase1）：高 = 阻塞（无法提交 / 必须回退 per_job）；
中 = 能跑通但有明显不便；低 = 体验小问题 / 文档不清。

| 用户 | 日期 | 问题描述 | 严重度（高/中/低） | 关联 submission_id / gpu_type | 处理与结论 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |
|  |  |  |  |  |  |
|  |  |  |  |  |  |

### 3.3 事件日志表（incident log）

记录灰度期间的异常事件（提交失败潮、worker 扩不起来、server 重启、log 流大面积断连、
触发回滚等）：

| 时间 | 现象 | 影响范围（用户/namespace/GPU 池） | 根因 | 处置（含是否回滚） | 状态（处理中/已解决） |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |
|  |  |  |  |  |  |
|  |  |  |  |  |  |

---

## 4. 判定与下一步

1–2 周窗口结束后，对照 §1 成功判定标准做综合评估，二选一：

### 4.1 通过 → 扩大白名单 → 推进任务 10.4（全量切换）

当满足全部硬指标（成功率 ≥ 95%、worker 启动时延中位 ≤ 90s / p95 ≤ 180s、autoscaling
正常 `0→N→0`、log 流无频繁断连、无未解决的高严重度反馈）：

- 在本文「结论」一节写明各指标实测值与「通过」判定。
- 先**扩大白名单**：对更多 namespace 执行
  `deploy/set-default-cluster-mode.sh shared --namespace <ns>`，再观察一小段时间。
- 稳定后进入任务 **10.4**：逐个 namespace 全量切换 shared（org-wide）。
- 全量后**永久保留** `--cluster-mode per_job` 作为应急回退（见 §6 与
  `docs/migration-shared-cluster.md` §3）。

### 4.2 不通过 → 回滚（见 §6）

任一硬指标不达标，或出现未解决的高严重度阻塞：暂缓 10.4，执行 §6 回滚预案，定位根因
（提交失败集中在某 gpu_type / token？worker 扩不起来是节点标签 / GPU 耗尽？log 流断连
是 Ingress / SSE 超时？），修复后重新开一轮灰度。

---

## 5. 监控值班速查（每日例行）

每天固定跑一遍，把结果落到 §3：

```bash
# 1) 成功率（近 24h）
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h | grep job_submit | grep -c '"outcome": "submitted"'
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h | grep job_submit | grep -c '"outcome": "error"'

# 2) worker 启动时延 / autoscaling：watch worker pod + 看 raycluster 状态
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
kubectl -n ray-shared describe raycluster ray-shared-h20 | tail -30

# 3) log 流：抽查一个当天的 submission 跟随一会儿
raytrain logs <submission_id> -f --gpu-type h20

# 4) server / 集群健康
kubectl -n raytrain-system rollout status deploy/raytrain-server
kubectl -n ray-shared get raycluster -l raytrain.shared=true
```

---

## 6. 回滚预案

回滚目标：让灰度用户立即回到 per_job（Phase 1）路径，不阻塞训练。per_job 作为
**永久应急回退**保留。操作层面三档手段，**与 `docs/migration-shared-cluster.md` §3
完全一致**（这里只摘要 + 交叉引用，命令以迁移文档为准）：

### 6.1 单次命令回退（用户侧，最快）— 见 migration §3.1

在任意一条命令加 `--cluster-mode per_job`，临时绕过 shared 走 K8s 直连：

```bash
raytrain submit --config <config> --gpus 8 --nodes 1 --cluster-mode per_job
raytrain logs   <rayjob-name> -f --cluster-mode per_job
raytrain stop   <rayjob-name>    --cluster-mode per_job
```

CLI flag 是最高优先级，会短路一切中间层。前提：该用户本机仍有可用 kubeconfig。

### 6.2 namespace 级回退（运维侧）— 见 migration §3.2

把整个 namespace 的默认模式切回 per_job，用户无需各自改本地配置：

```bash
deploy/set-default-cluster-mode.sh per_job --namespace <ns>

# 验证（应输出 per_job）
kubectl -n <ns> get configmap raytrain-defaults \
    -o jsonpath='{.data.default_cluster_mode}'
```

适合「某 namespace 的 shared 链路整体异常」（server 故障、长寿集群升级窗口）。

### 6.3 用户级回退（用户侧，持久）— 见 migration §3.3

在 `~/.raytrain/config.yaml` 把默认模式改回 `default_cluster_mode: per_job`，该用户
后续命令默认走 per_job。

> 优先级：CLI flag（§6.1）> namespace ConfigMap（§6.2）> 用户配置（§6.3）> 兜底 per_job。

### 6.4 触发回滚的条件

满足任一条即应回滚（至少回退受影响范围），并在 §3.3 事件日志表记录：

- 窗口累计 submission 成功率 **< 95%**，或任意单日 **< 90%** 且呈下降趋势。
- Submission_Server 宕机 / 不可用，shared 提交链路整体不通（`/healthz` 持续失败）。
- worker 普遍扩不起来或 autoscaling 卡死，导致任务长时间无法运行。
- log 流大面积断连 / SSE 持续 `log stream interrupted`，影响多名用户。
- 长寿集群进入 Ray 版本升级 drain 窗口（按
  `deploy/shared-cluster/README.md` 的 drain 步骤，需先停接受新 submission）。

回滚后在 §3.3 与「结论」里记录触发原因和影响范围，作为下一轮灰度的输入。

---

## 7. 结论（1–2 周窗口结束后填写）

> 灰度执行完成后在此写明：各指标实测值、是否达到 §1 成功判定标准、最终判定
>（通过 → 扩大白名单 / 推进 10.4，或不通过 → 回滚重来）、以及遗留问题清单。

- submission 成功率（窗口累计）：_待填_
- worker 启动时延（中位 / p95）：_待填_
- autoscaling 异常次数：_待填_
- log 流断连次数：_待填_
- 高严重度反馈：_待填_
- 最终判定：_待填（通过 → 扩大白名单 / 10.4 全量切换；不通过 → 回滚）_
