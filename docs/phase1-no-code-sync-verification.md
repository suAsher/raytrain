# Phase 1 · `--no-code-sync` 回退路径验证记录

对应 spec 任务 `long-term-evolution / 4.5`：验证 `--no-code-sync` 回退路径与
Phase 1 改造前（镜像内代码）行为一致。

本文记录两部分：

1. 可在本地完成的**自动化 + 静态代码路径**证据（已完成）。
2. 需要在 KubeRay + GPU 集群上执行的**实机 smoke 流程**（已编写 runbook，
   待有集群时由运维执行 —— 本地环境无法触达集群，故 deferred）。

---

## 1. 自动化证据：dry-run 测试

测试文件：`tests/test_cli_submit.py`，其中 `test_submit_dry_run_no_code_sync`
是任务 2.3 已落地的回归断言。

运行命令与结果：

```
PYTHONPATH=. python3 -m pytest tests/test_cli_submit.py -v
# tests/test_cli_submit.py::test_submit_dry_run_no_code_sync PASSED
# tests/test_cli_submit.py::test_submit_dry_run_with_code_sync PASSED
# 2 passed
```

`test_submit_dry_run_no_code_sync` 断言（即 `--no-code-sync` 等于旧行为）：

- `assert "/4]" in result.output`：输出为 **4 阶段** 进度（跳过打包 [1/5]
  与上传 [2/5]）。
- `assert "/5]" not in result.output`：不出现第 5 阶段，即没有 code-sync 专属阶段。
- `assert "working_dir:" not in result.output`：渲染出的 RayJob **不含
  `working_dir` 字段**，与 Phase 1 改造前镜像内代码模式一致。

对照组 `test_submit_dry_run_with_code_sync`（默认开启 code-sync）则断言 5 阶段
且 `working_dir:` 存在，从而证明两条路径行为确有区分、回退路径确实回到了旧形态。

---

## 2. 静态代码路径确认

### 2.1 CLI 侧：`raytrain/cli/submit.py`

`--no-code-sync` 使 `code_sync_enabled = False`，进而：

- `submit.py:157` `code_sync_enabled = manifest.code_sync.enabled and not no_code_sync`
  —— 传入 `--no-code-sync` 时恒为 `False`。
- `submit.py:158` `total_steps = 5 if code_sync_enabled else 4`
  —— 回退时总阶段数为 **4**。
- `submit.py:161-164` `code_uri` / `code_hash` 初始化为 `None`，`code_size_bytes=0`。
- `submit.py:165` `if code_sync_enabled:` 整段打包/上传逻辑（含 `build_code_zip`、
  `upload_code_zip`、`[1/..]`/`[2/..]` 阶段）被**跳过**，因此不会有任何打包或上传
  发生，`code_uri` 保持 `None`。
- `submit.py:255` 与 `submit.py:328`、`submit.py:359` 的阶段编号在
  `code_sync_enabled=False` 时分别取 `1/4`（MLflow）、`2/4`（render）、`3/4`（apply），
  对应改造前的 4 阶段顺序。
- `submit.py:322-325` 构建 `Plan` 时 `code_uri=code_uri`（即 `None`）、
  `code_hash=None`、`code_size_bytes=0`。
- `submit.py:331` `code_mode` 显示为 `"image-baked code"`。

### 2.2 模板侧：`raytrain/templates/rayjob.yaml.j2`

模板对 `working_dir` 与 `RAYTRAIN_CODE_*` 块都用 `{%- if code_uri %}` 包裹；
当 `Plan.code_uri` 为 `None` 时，渲染结果**不写 `working_dir`、不写
`config.setup_timeout_seconds`、不写 `RAYTRAIN_CODE_*` env_vars**，仅保留
`RAYTRAIN_USER` / `RAYTRAIN_JOB_NAME` / `RAYTRAIN_REPO` / `PYTHONUNBUFFERED`，
与改造前模板完全一致（dry-run 测试中 `working_dir:` 不出现即为佐证）。

### 2.3 Driver 侧：`raytrain/entrypoint/driver.py`

集群侧使用旧镜像（镜像内带代码）跑回退路径时：

- `_resolve_workdir`（`driver.py:74-101`）：当 `RAY_RUNTIME_ENV_WORKING_DIR`
  为空（无 working_dir 注入）时，回退到 `plan.workdir or manifest.workdir`
  （`driver.py:95-97`），即镜像内代码路径。两者都缺失才报错。
- code banner（`driver.py:104-126`）：无 `RAYTRAIN_CODE_HASH` 时输出
  `[driver] code_hash=<none>`（`driver.py:124`）。
- 主流程（`driver.py:391-401`）：`code_uri` 为空走 `else` 分支，打印
  `[driver] legacy code-in-image mode, workdir = <workdir>`。

> 说明：旧镜像内的 driver 没有 `RAYTRAIN_CODE_*` 处理逻辑，但因为
> `--no-code-sync` 提交的 RayJob 本身不注入 `working_dir` 也不注入
> `RAYTRAIN_CODE_*`，旧 driver 看到的环境与改造前完全一致，因此行为对齐。

---

## 3. 实机 smoke runbook（待集群可用时执行，当前 deferred）

> 本地（macOS / Python 3.9.6）无 KubeRay 集群与 GPU，无法执行实机 smoke。
> 以下为运维在有集群时的精确操作步骤，预期结果用于和 Phase 1 改造前对照。

### 3.1 前置

- 选用 **Phase 1 改造前的旧镜像 tag**（镜像里仍打包了训练代码），例如
  `--image <registry>/pointcept:<pre-phase1-tag>`。
- 复用任务 4.4 用过的同一个 smoke 配置（1×1 GPU、pointcept 跑到第一个 step）。
- 目标 dev namespace 与 4.4 相同。

### 3.2 提交命令

```bash
raytrain submit \
  --config <同 4.4 的 config> \
  --gpus 1 --nodes 1 --gpu-type h20 \
  --image <registry>/pointcept:<pre-phase1-tag> \
  --no-code-sync \
  --name smoke-no-code-sync
```

### 3.3 预期结果（与改造前一致）

1. **CLI 输出 4 阶段**（无 `[1/5] packaging` / `[2/5] uploading`）：
   - `[1/4] creating MLflow run ...`
   - `[2/4] rendering RayJob (... code: image-baked code)`
   - `[3/4] applying to namespace=...`
   - `[4/4] submitted`
2. **渲染的 RayJob 无 `working_dir`**（可 `--dry-run` 先核对，或
   `kubectl get rayjob <name> -o yaml | grep -c working_dir` 应为 0）。
3. **head pod 日志**出现：
   - `[driver] code_hash=<none>`
   - `[driver] legacy code-in-image mode, workdir = <镜像内 workdir>`
4. **训练日志出现 `loss=`**，到第一个 step 正常输出，与 4.4 的 working_dir
   模式结果一致（仅代码来源不同：镜像内 vs working_dir）。
5. MLflow run 中 `raytrain.code_uri` / `raytrain.code_hash` 为空字符串
   （回退路径不产生 code 元数据），符合改造前预期。

### 3.4 验收判定

若 3.3 全部满足，则 `--no-code-sync` 回退路径与 Phase 1 改造前行为一致，
任务 4.5 的实机部分通过。否则记录差异并回到 spec 复核。

---

## 结论

- 本地可验证部分**已通过**：dry-run 测试绿、CLI/模板/driver 三层静态路径均确认
  `--no-code-sync` 回到「无 `working_dir`、镜像内代码」的旧行为。
- 实机 smoke（旧镜像 + `--no-code-sync` 跑通同一 smoke run）**需 KubeRay + GPU
  集群**，本地环境无法触达，已按上文 runbook **deferred** 给运维执行。
