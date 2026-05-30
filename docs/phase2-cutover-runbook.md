# Phase 2 · 全量切换与 kubeconfig 退场运维手册（Cutover Runbook）

对应 spec 任务 `long-term-evolution / 10.4` 与 `10.5`，本手册把这两个**带时间门槛的
运维动作**串成一份可照着执行的 cutover runbook：

- **任务 10.4**：全量切换 `default_cluster_mode=shared`；保持 `--cluster-mode per_job`
  作为应急回退。（依赖 10.3）
- **任务 10.5**：10.4 稳定运行 **≥ 1 个月**后，停止给新用户下发 K8s kubeconfig，所有新
  用户走 token；旧 kubeconfig 按到期自然失效。（依赖 10.4）

> ⚠️ 重要说明（deferred）：10.4 / 10.5 都不是能在本地"执行"的代码任务，而是**面向真实
> 集群、按月推进的运维动作**（全组织切换 → 1 个月后停发 kubeconfig）。它们都**以灰度
> （任务 10.3）全部指标通过为前置门槛**。因此**实机执行被 deferred 给运维**，本文是
> 运维照做的 runbook：给出真实脚本路径与命令、验证方法、回滚预案，以及供逐项打勾的
> 空表格。

本文与下列文档共享术语与命令，请保持一致：

- `docs/phase2-rollout.md`（任务 10.3 · 灰度执行计划与数据采集模板，本文的**前置门槛**：
  其 §1 成功判定标准全部达标、§4.1 判定"通过"后才进入 10.4）。
- `docs/migration-shared-cluster.md`（任务 10.2 · 迁移步骤与**回退方法 §3**，本文全量切换
  公告与回滚直接引用它；新用户 token-only onboarding 见其 §1.3 / §1.4）。
- `deploy/set-default-cluster-mode.sh`（按 namespace 写 ConfigMap `raytrain-defaults`，
  全量切换 / namespace 级回滚都用它）。
- `deploy/issue-token.sh`（给新用户签发 HS256 JWT，token-only onboarding 用它）。
- `deploy/rbac/`（per-user kubeconfig / RBAC 签发工具，10.5 要逐步停用，见
  `deploy/rbac/README.md` 与 `deploy/rbac/add-user.sh`）。
- `raytrain/cli/configure.py`（`raytrain configure`，shared 模式无需 kubeconfig）。

---

## 1. 全量切换 default_cluster_mode=shared（任务 10.4）

把灰度（10.3）验证过的 shared 模式从"白名单 namespace"推到**全部活跃 namespace**，让
shared 成为全组织默认。`--cluster-mode per_job` 在本阶段**永久保留**为应急回退（不删除）。

### 1.1 前置条件（必须全部满足）

- 灰度（任务 10.3）窗口结束，`docs/phase2-rollout.md` §1 的**全部硬指标达标**且
  §4.1 判定为"通过"：
  - submission 成功率（窗口累计）**≥ 95%**，任意单日 **≥ 90%** 且无连续两天低于阈值；
  - worker 启动时延 中位 **≤ 90s**、p95 **≤ 180s**；
  - autoscaling 正常 `0 → N → 0`，无卡住；
  - log 流无频繁断连；
  - 无未解决的高严重度反馈。
- 长寿集群（`ray-shared`）head Running、Submission_Server（`raytrain-system` /
  `raytrain-server`）`/healthz` 正常（复用 `docs/phase2-rollout.md` 的就绪检查）。
- 已先"扩大白名单"观察一小段时间（`docs/phase2-rollout.md` §4.1 推荐的中间步），无新增
  回归。

任一前置不满足，**不进入全量切换**。

### 1.2 切换步骤：逐个 namespace 切到 shared

`set-default-cluster-mode.sh` 写 namespace ConfigMap `raytrain-defaults` 的
`default_cluster_mode` key，是 cluster-mode 解析的**中间层**
（优先级：CLI flag `--cluster-mode` > 本 ConfigMap > 用户配置 `~/.raytrain/config.yaml` >
兜底 `per_job`）。全量切换 = 对**所有活跃 namespace**把它写成 `shared`。

```bash
# 1) 先列出所有活跃 namespace（按你的实际筛选条件，例如有 raytrain-defaults 的、
#    或带某标签的；下面用"曾经 bootstrap 过 raytrain 的 ns"举例）
kubectl get ns -o name | sed 's#^namespace/##' > /tmp/raytrain-namespaces.txt
# 人工核对 /tmp/raytrain-namespaces.txt，去掉系统/无关 ns（如 kube-system、
# raytrain-system、ray-shared 这类不是用户提交 ns 的），只保留真正的用户 namespace。
```

```bash
# 2) 遍历 namespace 列表，逐个切到 shared（脚本幂等，可重复跑）
while read -r ns; do
  [ -z "$ns" ] && continue
  echo "=== switching ${ns} -> shared ==="
  deploy/set-default-cluster-mode.sh shared --namespace "$ns"
done < /tmp/raytrain-namespaces.txt
```

```bash
# 3) 逐个 namespace 验证（应输出 shared）
while read -r ns; do
  [ -z "$ns" ] && continue
  val="$(kubectl -n "$ns" get configmap raytrain-defaults \
      -o jsonpath='{.data.default_cluster_mode}' 2>/dev/null)"
  printf '%-24s default_cluster_mode=%s\n' "$ns" "${val:-<none>}"
done < /tmp/raytrain-namespaces.txt
```

> 建议**分批**推进（一次切几个 ns、观察 1–2 天再切下一批），而不是一次性全切，便于在
> 出现回归时把影响面控制住。每切一批就更新 §1.6 的切换表。

### 1.3 向用户公告

全量切换后向全体用户公告（邮件 / IM / 文档），要点：

- **shared 现在是默认模式**：新提交默认走 CLI → Submission_Server → 长寿 RayCluster，
  无需本机 kubeconfig（详见 `docs/migration-shared-cluster.md` §2.4）。
- **per_job 仍可用**：任何一条命令加 `--cluster-mode per_job` 即可临时走 K8s 直连作为
  **应急回退**（前提：本机仍有可用 kubeconfig）。这是 cluster-mode 解析的最高优先级，
  会短路一切中间层。命令示例与三档回退见 `docs/migration-shared-cluster.md` §3：

  ```bash
  raytrain submit --config <config> --gpus 8 --nodes 1 --cluster-mode per_job
  raytrain logs   <rayjob-name> -f --cluster-mode per_job
  raytrain stop   <rayjob-name>    --cluster-mode per_job
  ```

- shared 模式下 `logs` / `stop` 建议显式带 `--gpu-type`（见 migration §2.2）。

### 1.4 监控窗口与回滚

**监控窗口**：全量切换后持续监控（建议至少 1–2 周，且与 10.5 的"≥ 1 个月稳定期"衔接），
**复用 `docs/phase2-rollout.md` 的指标与每日例行检查**（§2 采集方法、§5 值班速查）：
submission 成功率、worker 启动时延、autoscaling 行为、log 流稳定性。把异常记入
`docs/phase2-rollout.md` §3.3 事件日志表。

**回滚路径**：若全量后出现回归，按影响面选择（命令以
`docs/migration-shared-cluster.md` §3 为准）：

```bash
# namespace 级回退：把某个（或全部）ns 的默认模式切回 per_job
deploy/set-default-cluster-mode.sh per_job --namespace <ns>

# 验证（应输出 per_job）
kubectl -n <ns> get configmap raytrain-defaults \
    -o jsonpath='{.data.default_cluster_mode}'
```

```bash
# 全组织回退：对 §1.2 的同一份 namespace 列表批量切回 per_job
while read -r ns; do
  [ -z "$ns" ] && continue
  echo "=== rolling back ${ns} -> per_job ==="
  deploy/set-default-cluster-mode.sh per_job --namespace "$ns"
done < /tmp/raytrain-namespaces.txt
```

用户侧最快的临时绕行仍是单条命令加 `--cluster-mode per_job`（migration §3.1）。触发回滚的
条件参照 `docs/phase2-rollout.md` §6.4。

### 1.5 重要：per_job 在本阶段永久保留

`--cluster-mode per_job` 在 10.4 阶段**永久保留、不删除**。按 design 的节奏，per_job 路径
要到**任务 10.6** 才会先标记 deprecated（CLI warning），再过 6 个月才删除该路径与对应
K8s 提交代码（即 10.5 之后 6+6 个月，见下方 §4 时间线）。因此：

- 不要在 10.4 / 10.5 阶段移除 `--cluster-mode per_job` 或 per_job 提交代码。
- 全量切到 shared 只是改"默认值"，per_job 作为应急回退始终在位。

### 1.6 全量切换记录表（逐 namespace 填写）

| namespace | 切换日期 (YYYY-MM-DD) | 状态（已切 shared / 待切 / 已回滚 per_job） | 备注（批次 / 异常 / 负责人） |
| --- | --- | --- | --- |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
| _（按需续行至覆盖全部活跃 namespace）_ |  |  |  |

---

## 2. 停止下发 kubeconfig（1 个月稳定后，任务 10.5）

10.4 全量稳定后，**新用户只走 token（shared）**，不再签发 per-user kubeconfig；**存量
kubeconfig 按到期自然失效**，不做批量吊销。

### 2.1 前置条件

- 任务 10.4 已完成，全组织 shared 默认**稳定运行 ≥ 1 个月**（监控窗口内无未解决的高严重度
  回归，指标持续达标）。
- Submission_Server + 长寿集群健康，token 签发链路（`deploy/issue-token.sh`）可用。

### 2.2 新用户 onboarding：仅用 token（不再发 kubeconfig）

新用户接入只需两步，**全程不签发 kubeconfig / 不跑 RBAC 工具**（流程与
`docs/migration-shared-cluster.md` §1.3 / §1.4 一致，配置项见 `raytrain/cli/configure.py`）：

```bash
# 1) 运维：给新用户签发一个 token（HS256 JWT，--tenant 按团队/租户划分）
deploy/issue-token.sh <user> --tenant <id>
#   默认有效期 30 天，可用 --days 调整；输出写 token-<user>.txt（权限 0600）
#   通过安全渠道发给本人（不要进 git / 群文件）
```

```bash
# 2) 用户：本机跑 raytrain configure，cluster-mode 选 shared，填 server URL + token
raytrain configure
#   交互提示里：
#     Cluster mode ...................... shared
#     Platform server URL (shared mode) . https://raytrain.internal.example.com
#     Platform token (shared mode) ...... <粘贴 token-<user>.txt 的内容>
#   shared 模式不需要 kubeconfig；configure 会提示
#   "shared mode ... no local kubeconfig/kubectl is required"
```

`shared_clusters`（gpu_type → ray head URL 映射）不在交互里 prompt，由平台运维提供 URL，
用户手动编辑 `~/.raytrain/config.yaml` 补上（见 migration §1.4）。

### 2.3 运维侧变化：停跑 per-user kubeconfig / RBAC 签发

10.5 生效后，对**新用户**：

- **停止运行** `deploy/rbac/add-user.sh <user> <namespace>`（它会建
  ServiceAccount + RoleBinding 并产出 `kubeconfig-<user>-<ns>.yaml`，是 per_job 用户的
  kubeconfig 签发入口，见 `deploy/rbac/README.md`）。
- 同理不再为新用户跑 `deploy/rbac/bootstrap-namespace.sh` 仅为了开通 per_job kubeconfig
  的流程。新用户一律走 §2.2 的 token-only onboarding。

对**存量用户**：

- **不做批量吊销**。已下发的 kubeconfig 让其**按到期自然失效**。`add-user.sh` 默认 token
  时长 1 年（`8760h`），到期后该 SA 名下的 token 自然失效；在此之前 per_job 仍可正常用。
- 因此**不要**对存量用户跑 `deploy/rbac/remove-user.sh`（删 ServiceAccount 会让其名下
  token 立即失效，那是"主动撤销"，不符合"自然到期"的策略）。
- 也**不主动**给存量用户跑 `deploy/rbac/rotate-token.sh` 续期；让旧 kubeconfig 走到到期
  即可（除非命中 §2.5 的应急情形）。

> per_job 路径本身**保持可用**（10.5 不动 per_job 代码，移除是更晚的任务 10.6）。
> 10.5 改变的只是"新用户怎么接入"和"是否主动续发 kubeconfig"，不是关闭 per_job。

### 2.4 新用户 onboarding 记录表（逐人填写）

| 用户 (user) | 租户 (tenant) | token 签发日期 (YYYY-MM-DD) | token 到期日 (YYYY-MM-DD) | 备注 |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |
|  |  |  |  |  |
|  |  |  |  |  |
|  |  |  |  |  |
| _（按需续行）_ |  |  |  |  |

### 2.5 风险与应急回退

- **存量用户 kubeconfig 已过期、又急需 per_job**：优先引导其改用 shared（绝大多数场景
  shared 已可满足）。确实必须 per_job 时，作为**应急**用现有 RBAC 工具临时补发一份
  kubeconfig（短时长即可），不破坏"自然到期"的整体策略：

  ```bash
  # 应急临时续发（短时长，例如 7 天 = 168h），用完即过期
  deploy/rbac/rotate-token.sh <user> <namespace> --duration 168h
  #   若该用户的 SA / RoleBinding 已不存在，则改用 add-user.sh 重新开通：
  #   deploy/rbac/add-user.sh <user> <namespace> --duration 168h
  ```

- **新用户 token 过期**：用 `deploy/issue-token.sh <user> --tenant <id>` 重新签发，用户
  `raytrain configure` 重填 token（migration §4 FAQ）。
- 保持务实：能用 shared 解决就不要回 per_job；per_job 只作为兜底，且其整体退场是任务
  10.6 的事。

---

## 3. 验证与监控

10.4 / 10.5 期间**复用 `docs/phase2-rollout.md` 的每日例行检查**（§5 值班速查 + §2 采集
方法），重点确认全量 / 停发 kubeconfig 后链路依旧健康：

```bash
# 1) submission 成功率（近 24h）
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h | grep job_submit | grep -c '"outcome": "submitted"'
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h | grep job_submit | grep -c '"outcome": "error"'

# 2) worker 启动时延 / autoscaling
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
kubectl -n ray-shared describe raycluster ray-shared-h20 | tail -30

# 3) log 流：抽查一个当天 submission 跟随一会儿
raytrain logs <submission_id> -f --gpu-type h20

# 4) server / 集群健康
kubectl -n raytrain-system rollout status deploy/raytrain-server
kubectl -n ray-shared get raycluster -l raytrain.shared=true
```

针对 10.5，额外做一次**新用户端到端验证**：确认一个**只有 token、没有 kubeconfig**的新
用户能完整跑通 shared 提交（onboarding → 提交 → 看日志），证明不发 kubeconfig 不影响接入：

```bash
# 模拟新用户：仅 token-only onboarding（§2.2），在没有 ~/.kube/config 的机器上
raytrain configure        # cluster-mode=shared，填 submission_server + token
raytrain submit --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 1 --nodes 1 --gpu-type h20 --name smoke-shared --dry-run
# --dry-run 看着没问题，去掉 --dry-run 真提交，再用 logs 跟随确认端到端通
raytrain logs <submission_id> -f --gpu-type h20
```

---

## 4. 时间线与里程碑

把 10.3 → 10.6 的整条推进弧串起来，便于运维看清各步的门槛与间隔：

| 任务 | 动作 | 时间门槛 / 间隔 | 前置依赖 | 状态 |
| --- | --- | --- | --- | --- |
| 10.3 | 5 名白名单灰度 `shared`，采集 4 项指标到 `docs/phase2-rollout.md` | 连续 **1–2 周**观测 | 8.4 / 9.2 | 已完成（`[x]`） |
| 10.4 | **全量切换** `default_cluster_mode=shared`（逐 ns）；`per_job` 永久保留为应急回退 | 灰度判定"通过"后；建议分批 + 监控 1–2 周 | 10.3 | 本手册 §1（待运维执行） |
| 10.5 | 停止给**新用户**下发 kubeconfig，新用户走 token；旧 kubeconfig **自然到期失效** | 10.4 全量**稳定 ≥ 1 个月**后 | 10.4 | 本手册 §2（待运维执行） |
| 10.6 | `--cluster-mode per_job` 标记 **deprecated**（CLI warning），再 **6 个月**后删除该路径与 K8s 提交代码 | 10.5 之后 **6 + 6 个月** | 10.5 | **未来 / 暂不执行**（本手册不涵盖） |

> 注：10.6 是更远期的 per_job 退场，**本手册不涉及**，仅在此标出以呈现完整节奏。在 10.4 /
> 10.5 阶段，per_job 与存量 kubeconfig 必须保持可用。
