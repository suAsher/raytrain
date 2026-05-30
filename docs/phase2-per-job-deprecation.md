# Phase 2 · per_job 路径废弃与移除计划（Deprecation & Removal Plan）

对应 spec 任务 `long-term-evolution / 10.6`（依赖 10.5）：

> 6 个月后把 `--cluster-mode per_job` 标记 deprecated（CLI warning），再 6 个月删除该
> 路径与对应 K8s 提交代码。

本文是这条**远期、按月推进**的退场弧的权威说明：交代背景、时间线、运维如何开启
deprecation warning、未来移除阶段要删/收口的清单，以及回退方式。

> ⚠️ 重要（deferred）：这是一条**未来才执行**的时间门槛任务。到本文写作时，**什么都还
> 没有移除**：per_job 路径完整可用，是 10.4 / 10.5 全程的**应急回退**。代码侧目前只落地
> 了一个**默认关闭、可由运维开启**的 deprecation warning 开关（见 §3），其余（移除 per_job
> 代码、收口 K8s 提交链路）都是**将来的 checklist**（见 §4），现在不做。

本文与下列文档共享术语与节奏，请保持一致：

- `docs/phase2-cutover-runbook.md`（任务 10.4 / 10.5 运维手册，其 **§4 时间线**把
  10.3 → 10.6 串成一条推进弧，本文是该时间线里 10.6 这一格的展开）。
- `docs/migration-shared-cluster.md`（任务 10.2 · 迁移步骤与**回退方法 §3**，deprecation
  warning 文案指向它的 shared 迁移指引）。
- `raytrain/cli/submit.py`（`--cluster-mode` 选项、`_resolve_cluster_mode`、以及本任务新增
  的 `_maybe_warn_per_job_deprecated` 开关）。

---

## 1. 背景与前置

- **per_job 是遗留路径**：CLI 直连 K8s API，每个任务现起一个 per-job RayCluster，依赖每
  用户的 kubeconfig（见 `docs/migration-shared-cluster.md` §2.4）。
- **shared 已是默认（任务 10.4）**：全量切换后 `default_cluster_mode=shared`，新提交走
  CLI → Submission_Server → 长寿 RayCluster，无需本机 kubeconfig。`--cluster-mode per_job`
  在 10.4 阶段**永久保留**为应急回退。
- **kubeconfig 已停止下发（任务 10.5）**：10.4 稳定 ≥ 1 个月后，新用户只走 token，存量
  kubeconfig 按到期**自然失效**，不做批量吊销。
- **本任务（10.6）是最终落幕**：在 10.5 稳定之后，先把 per_job **标记 deprecated**（开启
  CLI warning），再过 6 个月**删除** per_job 代码路径与对应 K8s 提交代码。

退场的前提是 shared 已经把绝大多数场景接住、per_job 的真实使用量降到可忽略，且存量
kubeconfig 已基本到期。**任一前提不满足都不要推进移除。**

---

## 2. 时间线

以 **T0 = 任务 10.5 稳定**为锚点（不是 10.4），逐格推进。与
`docs/phase2-cutover-runbook.md` §4 的时间线表互为引用。

| 里程碑 | 时间门槛 | 动作 | 前置依赖 | 状态 |
| --- | --- | --- | --- | --- |
| **T0** | 任务 10.5 稳定运行 | per_job 仍是默认回退、完整可用；deprecation warning **默认关闭** | 10.5 | 现状 |
| **T0 + 6 个月** | 自 T0 起满 6 个月 | **标记 deprecated**：把 `RAYTRAIN_PERJOB_DEPRECATED=1` 下发到用户环境 / 共享 CLI 镜像，使 per_job 提交打印 CLI warning；同时向全员公告退场计划 | T0 | 未来 / 暂不执行 |
| **T0 + 12 个月** | 标记 deprecated 后再满 6 个月 | **移除**：删除 `submit.py` 的 per_job 分支与对应 K8s 提交代码，按 §4 清单收口 | T0 + 6mo | 未来 / 暂不执行 |

> 这与 cutover runbook §4 里"10.6 = 10.5 之后 **6 + 6 个月**"完全一致：第一个 6 个月到
> 标记 deprecated，第二个 6 个月到代码移除。

---

## 3. 如何开启 deprecation warning（运维动作）

到 **T0 + 6 个月**真正进入 deprecation 窗口时，运维只需翻一个**环境变量开关**——不改
代码、不动 per_job 功能。

### 3.1 开关：环境变量 `RAYTRAIN_PERJOB_DEPRECATED`

`raytrain/cli/submit.py` 里的 `_maybe_warn_per_job_deprecated(resolved_mode)` 只有在
**同时满足**下面两个条件时才打印 warning：

1. 解析出的模式是 `per_job`（shared 提交永远不会被打扰）；
2. 环境变量 `RAYTRAIN_PERJOB_DEPRECATED` 为**真值**：`1` / `true` / `yes` / `on`
   （大小写不敏感）；未设置或其它值都视为**关闭**。

**默认（不设置该变量）= 完全无副作用**，per_job 行为与现有全部测试都不变。这正是 per_job
作为 10.4 / 10.5 应急回退期间"不要每次都唠叨用户"的设计。

### 3.2 开启方式

把变量设到用户环境或共享 CLI 镜像里即可（任选其一）：

```bash
# 方式 A：临时（单个 shell / 单次提交）
export RAYTRAIN_PERJOB_DEPRECATED=1
raytrain submit --config <config> --gpus 8 --nodes 1 --cluster-mode per_job
#   stderr 会多打印一行黄色 warning（见下）

# 方式 B：共享 CLI 镜像 / 全员环境（推荐，进入 deprecation 窗口时统一开启）
#   在 CLI 镜像的 Dockerfile 或团队 shell profile 里加：
#     ENV RAYTRAIN_PERJOB_DEPRECATED=1
#   或 /etc/profile.d/raytrain.sh: export RAYTRAIN_PERJOB_DEPRECATED=1
```

### 3.3 warning 文案（精确文本）

开启后，每次 per_job 提交会向 **stderr** 打印一行（黄色）：

```
warning: --cluster-mode per_job is deprecated and will be removed; migrate to shared mode (see docs/migration-shared-cluster.md).
```

文案与 shared 迁移指引（`docs/migration-shared-cluster.md`）对齐，引导用户迁移。开启开关
后还应配合**全员公告**（邮件 / IM / 文档），说明 per_job 将于 T0 + 12 个月移除、请尽快迁到
shared。

---

## 4. 移除阶段清单（T0 + 12 个月，未来 checklist）

> ⚠️ 下面是**将来**到 T0 + 12 个月移除里程碑时才执行的 checklist，**现在不做**。列在这里是
> 为了让那时的执行者有据可依。

### 4.1 移除前置条件（必须全部满足）

- deprecation warning 已开启**满 6 个月**且全员已公告。
- **没有活跃的 per_job 用户**：监控/审计确认近期已无 per_job 提交。
- **所有 kubeconfig 已到期**：存量 per-user kubeconfig 都已自然失效（任务 10.5 的策略），
  没有人还在靠 kubeconfig 直连 K8s。
- shared 链路稳定，没有需要 per_job 兜底的未决场景。

任一不满足，**推迟移除**，继续留在 deprecated（warning）状态。

### 4.2 删除 / 收口清单

- **`raytrain/cli/submit.py` 的 per_job 分支**：移除 `resolved_mode == "per_job"` 之后的
  整段 K8s 直连逻辑（package/upload、`create_run`、render、`apply_yaml_docs` 等），以及
  `_maybe_warn_per_job_deprecated` 开关本身。
- **K8s apply / 提交链路**：清理 `submit.py` / `status.py` / `logs.py` 中对
  `raytrain/kube.py` 的使用（`apply_yaml_docs`、`load_kube`、`delete_rayjob`、
  `list_rayjobs`、`get_rayjob`、`list_pods_for_rayjob`、`stream_pod_logs` 等仅服务于
  per_job 的入口），相应精简 `kube.py`。
- **`--cluster-mode` 选项**：三个命令（`submit` / `list` / `stop` / `logs`）的
  `--cluster-mode` 选择项收敛为**仅 shared**，或直接移除该选项（shared 成为唯一路径）。
  同步调整 `_resolve_cluster_mode` 的兜底值（不再兜底到 per_job）。
- **per-user RBAC / kubeconfig 工具退役**：`deploy/rbac/`（`add-user.sh`、
  `bootstrap-namespace.sh`、`remove-user.sh`、`rotate-token.sh`、`list-users.sh`、
  `role.yaml`、`resource-quota.yaml` 等）签发 per_job kubeconfig 的工具链整体退役/归档。
- **文档更新**：更新 `docs/migration-shared-cluster.md`（删去 per_job 回退章节或改注
  "已移除"）、`docs/phase2-cutover-runbook.md`（标记 10.6 完成）、用户/运维指南里关于
  `--cluster-mode per_job` 的描述。
- **测试更新**：移除/改写仅针对 per_job 的测试（含本任务的 deprecation 测试与
  per_job dry-run 测试）。

---

## 5. 回退

- **deprecation 阶段（T0 + 6mo 起）出问题**：直接**取消设置** `RAYTRAIN_PERJOB_DEPRECATED`
  （或设为非真值），warning 立即消失。per_job 功能**完全不受影响**，仍可正常提交——开关只
  控制"是否打印提醒"，不改变任何行为。

  ```bash
  unset RAYTRAIN_PERJOB_DEPRECATED        # 或从共享镜像 / profile 里移除该 ENV
  ```

- **移除阶段（T0 + 12mo）之前**：per_job 始终是 backstop。只要还没到移除里程碑、移除
  前置条件还没满足，就保持 per_job 可用，不要提前删代码。
- 真正移除后若仍需 K8s 直连，属于"复活遗留路径"，应走正式回滚（revert 移除提交）并重新
  评估 shared 是否真的不能满足需求，而非临时绕行。
