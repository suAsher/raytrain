# Release Notes · Phase 1 全量推开 code-as-submission（working_dir）

对应 spec 任务 `long-term-evolution / 5.2`：在灰度（任务 5.1）通过后全量推开
`working_dir` 默认值，并在 release notes 中注明**默认行为变更**与**回退方法**。

> 说明：本仓库代码层面的「默认开启」早已就位（见下「默认行为变更」一节的代码佐证），
> 本次 5.2 的「全量推开」是一次**面向全员的发布动作**——把默认从灰度 namespace
> 扩到所有用户。组织级公告 / 通知属于运维操作步骤，本文是配套的发布说明，供公告
> 直接引用。

---

## 默认行为变更

**从此版本起，`raytrain submit` 默认启用 code-as-submission。**

提交流程变为：打包当前工作目录 → 上传 MinIO `raytrain-code` bucket → 集群侧 Ray
通过 `runtime_env.working_dir` 自动拉取并解压、`chdir` 进去再跑训练。**改代码不再
需要重建镜像**，`raytrain submit` 重新提交即可生效；镜像从此只承载环境（依赖 +
raytrain 本体），只有改依赖时才需要重 build。

代码佐证（默认即为 ON，无需任何额外开关）：

- `raytrain/manifest.py` · `CodeSync.enabled: bool = True`——dataclass 默认开启；
  `_load_code_sync()` 在 `.raytrain.yaml` 缺省 `code_sync` 块时返回
  `CodeSync()`（`enabled=True`），有块但缺 `enabled` 字段时
  `enabled=bool(raw.get("enabled", True))` 同样默认 `True`。
- `raytrain/cli/submit.py` ·
  `code_sync_enabled = manifest.code_sync.enabled and not no_code_sync`——只要
  不显式传 `--no-code-sync` 且未在 manifest 设 `code_sync.enabled: false`，提交
  即走 working_dir 路径（5 阶段进度）。

对应需求：Requirement 5 验收 2（缺省 `code_sync` 字段默认 `enabled: true`，且默认
值变更须在 release notes 中明确标记——即本节）。

---

## 影响面

- **日常提交多了两个阶段**：进度从原来的 4 阶段变为 **5 阶段**——
  `[1/5] packaging code` / `[2/5] uploading code` / `[3/5] creating MLflow run` /
  `[4/5] rendering RayJob` / `[5/5] applying`。打包 + 上传通常数秒到十几秒，取决于
  代码体量与网络。
- **依赖 admin 预建 bucket**：集群需存在 `raytrain-code` bucket 并配 7 天
  lifecycle。由 `deploy/setup-code-bucket.sh` 完成（幂等，详见下「升级/部署注意」）。
  CLI 在提交时也会 best-effort 兜底建桶，但**正式环境应由 admin 预先跑脚本**以确保
  lifecycle 生效。
- **镜像可瘦身**：环境镜像不再需要 `COPY` 训练代码（详见「升级/部署注意」）。

---

## 排除规则与上限

- **默认排除**：`.git/`、`.venv/` / `venv/`、`__pycache__/` / `*.pyc`、各类 cache
  （`.pytest_cache/` / `.mypy_cache/` / `.ruff_cache/`）、IDE 目录
  （`.idea/` / `.vscode/`）、`node_modules/`、数据与产物目录
  （`data/` / `datasets/` / `exp/` / `logs/`）、checkpoint 与压缩包
  （`*.ckpt` / `*.pth` / `*.pt` / `*.tar` / `*.zip`），以及仓库根 `.gitignore`
  中列出的条目。数据目录默认不会被打进 zip。
- **`.raytrainignore` 可追加**：在仓库根新建 `.raytrainignore`（gitignore 风格），
  每行作为 pattern 追加到排除规则；也可在 `.raytrain.yaml` 写
  `code_sync.extra_excludes: [...]`。
- **单 zip ≤ 200 MiB**：上限由 `.raytrain.yaml` 的 `code_sync.max_size_mib`
  控制（默认 200）。超限时 CLI 终止提交并打印当前大小、上限、体积前 10 的文件，
  按提示用 `.raytrainignore` 排除大文件后重试。

细节见 `docs/user-guide.md` §9.2 / §9.3 / §9.4。

---

## 复现

- 每次提交会把 code 元数据写入 MLflow run tags：`raytrain.code_uri`、
  `raytrain.code_hash`（zip 内容 SHA256）、`raytrain.code_size_bytes`。
- 配套子命令 `raytrain reproduce <mlflow_run_id>`（关联任务 11.1）可按 tag 中的
  `code_uri` 从 MinIO 重新下载对应 zip 还原代码。注意 7 天 lifecycle：超过保留期
  的对象无法恢复，需改用 git commit 回溯。

细节见 `docs/user-guide.md` §9.5。

---

## 回退方法

`--no-code-sync` 回退路径**永久保留**，回退后行为与 Phase 1 改造前（镜像内代码）
完全一致（验证记录见 `docs/phase1-no-code-sync-verification.md`）。三档手段：

- **单次回退（用户侧，最快）**——提交时加 `--no-code-sync`：

  ```bash
  raytrain submit --config <config> --gpus 8 --nodes 1 --gpu-type h20 \
      --no-code-sync
  ```

  此时跳过 `[1/5]` / `[2/5]`，渲染出的 RayJob 不含 `working_dir`，用镜像内代码。

- **项目级回退（仓库侧）**——在 `.raytrain.yaml` 写：

  ```yaml
  code_sync:
    enabled: false
  ```

  该仓库后续提交默认走镜像内代码模式，等价于每次都带 `--no-code-sync`。

- **per_job / legacy 路径继续保留**：本次仅变更代码同步默认值，提交链路
  （per_job RayCluster / K8s 直连）保持不变；`--cluster-mode per_job` 仍是默认。

---

## 升级/部署注意

- **admin 必做**：在目标 MinIO 上执行一次建桶脚本（幂等，可重复跑）：

  ```bash
  MINIO_ENDPOINT=http://<minio-host>:<port> \
  MINIO_ACCESS_KEY=<ak> \
  MINIO_SECRET_KEY=<sk> \
      ./deploy/setup-code-bucket.sh
  # 验证 7 天 lifecycle（应出现 Days: 7 的过期规则）：
  mc ilm export raytrain-setup/raytrain-code
  ```

  bucket 名、lifecycle、配额建议与紧急清理见 `docs/ops-guide.md` §9。

- **镜像可瘦身**：环境镜像不再需要把训练代码 `COPY` 进去；保留也无妨。建议保留
  环境镜像可正常构建，以便 `--no-code-sync` 回退路径随时可用。

---

## 交叉引用

- `docs/user-guide.md` §9 «代码同步与镜像»——排除规则、`.raytrainignore`、
  200 MiB 上限、Code_Hash 与 MLflow tag、7 天保留、镜像只放环境。
- `docs/ops-guide.md` §9 «Code Bucket 运维»——bucket 名、lifecycle、配额、紧急
  清理。
- `docs/quickstart.md` 第 5 步（`raytrain submit`）——默认打包上传、`--no-code-sync`
  回退一句话说明。
- `docs/phase1-rollout.md`——任务 5.1 灰度计划与判定标准（推开前置依据）。
- `docs/phase1-no-code-sync-verification.md`——`--no-code-sync` 回退路径验证记录。
