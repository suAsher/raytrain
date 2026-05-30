# Phase 1 · 灰度推开计划与数据采集模板

对应 spec 任务 `long-term-evolution / 5.1`：在 1 个 namespace 灰度 3 天，收集
zip 大小分布、解压失败率、用户反馈，并据此判定是否全量推开（任务 5.2）。

> ⚠️ 重要说明（deferred）：本文是**灰度执行计划 + 数据采集模板**。实际的「3 天
> 灰度数据收集」需要一套带 KubeRay + GPU 的活跃集群、真实用户、跨 3 天的连续观测，
> 本地环境无法完成。因此**实机 3 天灰度被 deferred 给运维在真实金丝雀（canary）
> 窗口执行**。运维按本文的命令采集数据、把结果填进下面的空表格，并据「判定与下一步」
> 一节决定推开或回滚。前置依赖：任务 4.4（working_dir smoke 通过）、任务 4.5
> （`--no-code-sync` 回退路径已验证，记录见 `docs/phase1-no-code-sync-verification.md`）。

本文与下列文档共享术语与命令，请保持一致：

- `docs/ops-guide.md` §9 Code Bucket 运维（bucket 名 `raytrain-code`、`mc` 别名
  `raytrain-setup`、lifecycle 7 天）。
- `docs/phase1-no-code-sync-verification.md`（回退路径验证，回滚时引用）。
- `deploy/setup-code-bucket.sh`（建桶脚本，alias = `raytrain-setup`）。

---

## 1. 灰度目标与范围

### 目标

验证 Phase 1（code-as-submission / `working_dir` 同步）在真实用户、真实代码体量下
稳定可用，把它从「smoke 通过」推进到「可全量默认开启」。

### 范围

| 项 | 值 |
| --- | --- |
| 灰度 namespace | **1 个**（建议 `ray-cluster-3`，与 ops-guide 示例一致；按实际选定后填入下表） |
| code-sync 默认 | **ON**（`code_sync.enabled` 默认 `true`，即不带 `--no-code-sync`） |
| 观察窗口 | **连续 3 天**（记 Day 1 / Day 2 / Day 3） |
| 回退手段 | 始终保留 `--no-code-sync`（详见 §5 回滚预案） |
| 灰度用户 | 该 namespace 下的全部活跃提交用户 |

灰度开始前确认：

```bash
# 灰度 namespace（按实际替换 ray-cluster-3）
kubectl get ns ray-cluster-3

# code bucket 已建好、lifecycle = 7 天（应出现 Days: 7 的过期规则）
mc ilm export raytrain-setup/raytrain-code
```

### 成功判定标准（3 天窗口结束时综合评估）

| 指标 | 阈值（满足即视为通过） |
| --- | --- |
| 解压失败率 | **≤ 2%**（`解压失败任务数 / 总提交数`，3 天累计） |
| 单日解压失败率 | 任意单日 **≤ 5%**，且无持续上升趋势 |
| zip 大小 | p95 **≤ 200 MiB**（不触达硬上限），且无大量任务因超限被客户端拒绝 |
| 用户反馈 | **无阻塞性（严重度 = 高）问题**未解决 |

任一硬指标不达标，进入 §5 回滚预案；全部达标则推进任务 5.2 全量推开。

---

## 2. 监控指标与采集方法

三个核心指标：**zip 大小分布**、**解压失败率**、**用户反馈**。下面给出可直接复制
粘贴的命令。命令里的 `<alias>` 统一用 `raytrain-setup`（见 `setup-code-bucket.sh`），
namespace 统一用 `ray-cluster-3`（按实际替换）。

### 2.1 zip 大小分布

**来源 A · MinIO（最权威，覆盖所有提交）**

```bash
# 看灰度窗口内某用户/全部用户的 zip 列表与单文件大小
mc ls --recursive raytrain-setup/raytrain-code/

# bucket 总用量（粗看整体规模）
mc du raytrain-setup/raytrain-code

# 汇总行数 + 总量
mc ls --recursive --summarize raytrain-setup/raytrain-code/
```

把 `mc ls --recursive` 的每行大小汇总成分布桶（建议分档：
`<10 / 10-50 / 50-100 / 100-200 MiB`，以及 `>200`（应为 0，超限会被客户端拦下）。
可用下面的一行脚本把当天 zip 大小归桶（`mc ls --json` 输出含 `size` 字段，单位字节）：

```bash
mc ls --recursive --json raytrain-setup/raytrain-code/ \
  | python3 -c 'import sys,json; b=[0]*5; lab=["<10","10-50","50-100","100-200",">200"]; \
[ ( (lambda m: b.__setitem__(0 if m<10 else 1 if m<50 else 2 if m<100 else 3 if m<200 else 4, \
b[0 if m<10 else 1 if m<50 else 2 if m<100 else 3 if m<200 else 4]+1))(json.loads(l)["size"]/1048576) ) \
for l in sys.stdin if l.strip() ]; print("\n".join(f"{lab[i]} MiB: {b[i]}" for i in range(5)))'
```

> 注意：MinIO 端的对象只在 7 天 lifecycle 窗口内可见，灰度只有 3 天，期间不会被
> lifecycle 清掉，可放心按天采集。

**来源 B · `raytrain submit` 客户端输出（按提交实时记录）**

客户端在打包阶段会打印一行（见 `raytrain/cli/submit.py`）：

```text
      zip size: 87.3 MiB, sha256: a3f8c1d2e4b5..., files: 1234
```

请用户在反馈时附上这行，或运维从 CI / 提交日志里 `grep`：

```bash
# 若提交日志被集中收集，可直接 grep 客户端打印的大小行
grep -E "zip size: [0-9.]+ MiB" <汇总的提交日志文件>
```

把两来源对齐后，按天填入 §3 的每日指标表（平均 / 中位 / p95）。

### 2.2 解压失败率

定义：

```text
解压失败率 = 解压失败任务数 / 总提交数（同一天 / 整个 3 天窗口）
```

「解压失败」= Ray 在拉取 / 解压 `runtime_env.working_dir`（即 code zip）阶段失败，
导致 head/worker 起不来或 RayJob 直接失败。采集方法：

**(1) 统计总提交数**

```bash
# 灰度 namespace 内 3 天产生的 RayJob 总数（per_job 模式）
kubectl -n ray-cluster-3 get rayjob \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.jobDeploymentStatus,CREATED:.metadata.creationTimestamp
```

也可结合 MinIO 侧 zip 数量（`mc ls --recursive | wc -l`）做交叉校验。

**(2) 找出解压失败任务**

working_dir 拉取 / 解压失败通常体现在 head/worker pod 的 events 与日志里。关键词：
`working_dir`、`setup_timeout`、`runtime_env`、`unzip`、`Failed to download`、
`Failed to setup runtime environment`。

```bash
# a) namespace 级 events，按时间排序，挑 runtime_env / working_dir 相关
kubectl -n ray-cluster-3 get events --sort-by='.lastTimestamp' \
  | grep -Ei 'working_dir|runtime_env|setup_timeout|unzip|download'

# b) 某个可疑任务的 RayJob 状态（失败会是 FAILED）
kubectl -n ray-cluster-3 get rayjob <job-name> \
  -o jsonpath='{.status.jobDeploymentStatus}{"  "}{.status.jobStatus}{"\n"}'

# c) head pod 日志里抓 runtime_env setup 报错
kubectl -n ray-cluster-3 logs <head-pod> 2>&1 \
  | grep -Ei 'working_dir|runtime env|setup_timeout|Failed to (download|setup)'

# d) 直接用 raytrain 看日志
raytrain logs <job-name>
```

把「RayJob = FAILED 且失败根因落在 code 拉取 / 解压」的任务计为「解压失败」。注意
排除与 code-sync 无关的失败（GPU 不够、训练代码本身 bug、数据集缺失等）——这些不计入
解压失败率，但可在备注里标注，避免误判。

> 交叉验证：`docs/ops-guide.md` §6「任务一直 Pending / placement group not ready」
> 里的多数原因（GPU、节点标签、hostPath）**不属于**解压失败，分流时参考该节。

### 2.3 用户反馈

用轻量模板收集，重点是「是否阻塞」。每条反馈一行，严重度分 高 / 中 / 低：

- **高**：无法提交 / 任务必然失败 / 必须回退到 `--no-code-sync` 才能跑。
- **中**：能跑通但有明显不便（如 zip 偏大、打包慢、需要手写 `.raytrainignore`）。
- **低**：体验小问题 / 文档不清。

反馈渠道：群消息 + 直接填 §3 的用户反馈表。每条尽量附上 `job_name` 与客户端打印的
`zip size` / `sha256` 前 12 位，便于定位对应 MinIO 对象与 RayJob。

---

## 3. 数据记录表格（灰度期间填写）

> 以下为空模板，运维在真实金丝雀窗口逐日填入。namespace、日期按实际替换。

灰度元信息：

| 项 | 值 |
| --- | --- |
| 灰度 namespace | _（待填，如 ray-cluster-3）_ |
| 灰度起止 | _（待填：Day1 起 ~ Day3 止）_ |
| raytrain 版本 / commit | _（待填）_ |
| 镜像 tag | _（待填，env-only 镜像）_ |
| 负责人 | _（待填）_ |

### 3.1 每日指标表

| 日期 | 提交数 | 平均 zip (MiB) | 中位 zip (MiB) | p95 zip (MiB) | 解压失败数 | 解压失败率 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Day 1 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |
| Day 2 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |
| Day 3 _(YYYY-MM-DD)_ |  |  |  |  |  |  |  |
| **3 天累计** |  |  |  |  |  |  |  |

### 3.2 zip 大小分布表（3 天累计）

| 区间 | 任务数 | 占比 |
| --- | --- | --- |
| < 10 MiB |  |  |
| 10–50 MiB |  |  |
| 50–100 MiB |  |  |
| 100–200 MiB |  |  |
| > 200 MiB（应为 0，超限被客户端拦截） |  |  |

### 3.3 用户反馈表

| 用户 | 日期 | 问题描述 | 严重度（高/中/低） | 关联 job / code_hash | 处理与结论 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |
|  |  |  |  |  |  |
|  |  |  |  |  |  |

---

## 4. 判定与下一步

3 天窗口结束后，对照 §1 成功判定标准做综合评估，二选一：

### 4.1 通过 → 推进任务 5.2（全量推开）

当满足全部硬指标（解压失败率 ≤ 2%、p95 zip ≤ 200 MiB、无未解决的高严重度反馈）：

- 在本文「结论」一节写明各指标实测值与「通过」判定。
- 进入任务 **5.2**：全量推开 `working_dir` 默认值，并在 release notes 注明默认行为
  变更与 `--no-code-sync` 回退方法。
- 全量后仍永久保留 `--no-code-sync` 作为应急回退开关。

### 4.2 不通过 → 回滚（见 §5）

任一硬指标不达标，或出现未解决的高严重度阻塞：暂缓 5.2，执行 §5 回滚预案，
定位根因（解压失败集中在某类代码体量？某个 namespace 网络？setup 超时？），
修复后重新开一轮 3 天灰度。

---

## 5. 回滚预案

回滚目标：让灰度 namespace 的用户立即回到 Phase 1 改造前的「镜像内代码」行为，
不阻塞训练。回退路径已由任务 4.5 验证（见
`docs/phase1-no-code-sync-verification.md`），这里给操作层面三档手段。

### 5.1 单次提交回退（用户侧，最快）

让用户在 `raytrain submit` 时加 `--no-code-sync`：

```bash
raytrain submit --config <config> --no-code-sync ...
```

此时跳过打包 / 上传，渲染出的 RayJob 不含 `working_dir`，等价于改造前行为。
适合「个别用户 / 个别任务踩到 working_dir 故障」的临时绕行。

### 5.2 项目级回退（仓库侧）

在该仓库的 `.raytrain.yaml` 写：

```yaml
code_sync:
  enabled: false
```

该仓库后续所有提交默认走镜像内代码模式，不必每次手敲 `--no-code-sync`。适合
「某个项目的代码暂不适配 working_dir」。

### 5.3 触发回滚的条件

满足任一条即应回滚（至少回退受影响范围）：

- 3 天累计解压失败率 **> 2%**，或任意单日 **> 5%** 且呈上升趋势。
- 出现高严重度（阻塞性）反馈且短期无法修复。
- code bucket / MinIO 侧异常（如 lifecycle 误删、容量打满）导致 working_dir
  普遍拉取失败 —— 此时配合 `docs/ops-guide.md` §9「紧急清理 / 验证命令」排查。

回滚后在 §3.3 用户反馈表与「结论」里记录触发原因和影响范围，作为下一轮灰度的输入。

---

## 6. 结论（3 天窗口结束后填写）

> 灰度执行完成后在此写明：各指标实测值、是否达到 §1 成功判定标准、最终判定
>（通过推进 5.2 / 回滚重来）、以及遗留问题清单。

- 解压失败率（3 天累计）：_待填_
- zip 大小 p95：_待填_
- 高严重度反馈：_待填_
- 最终判定：_待填（通过 → 5.2 / 不通过 → 回滚）_
