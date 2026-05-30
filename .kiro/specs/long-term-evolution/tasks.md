# Implementation Plan

每条任务都设计成可独立执行 + 可独立验证。完成后请勾选并填写验证记录。

## Phase 1 · 代码免镜像同步

### 1. 客户端打包与上传基础设施

- [x] 1.1 在 `pyproject.toml` 中新增依赖 `pathspec>=0.11`，并在 `make install` 后
  通过 `python -c "import pathspec; print(pathspec.__version__)"` 验证可导入。
  - _依赖：无_
  - _验收：CI / 本地 `pip install -e .` 成功；`pathspec` 可正常 import。_

- [x] 1.2 创建 `raytrain/code_sync.py`，实现 `CodeBundle` dataclass、
  `DEFAULT_IGNORES`、`build_code_zip(workdir, job_name, user, extra_excludes,
  max_size_bytes)` 三件，能根据默认 + `.raytrainignore` + `extra_excludes`
  生成 zip，并在大小超限时抛 `CodeSyncTooLargeError`（携带 top-10 大文件信息）。
  - _依赖：1.1_
  - _验收：`tests/test_code_sync.py::test_build_zip_excludes_default`、
    `test_size_limit_exceeded`、`test_sha256_stable` 全部通过。_

- [x] 1.3 在 `raytrain/code_sync.py` 中实现 `upload_code_zip(bundle, minio_client,
  bucket="raytrain-code", dedup=False)`，包含 5xx / 网络错误 3 次重试 + 指数
  退避；4xx 直接抛错。
  - _依赖：1.2_
  - _验收：`tests/test_code_sync.py::test_upload_retries_on_5xx`、
    `test_upload_no_retry_on_4xx` 通过（用 `pytest-mock` 替换 minio client）。_

- [x] 1.4 在 `raytrain/manifest.py` 中加入 `CodeSync` dataclass + `Manifest.code_sync`
  字段；`Manifest.load` 中处理缺省值（`enabled=True` 默认）。
  - _依赖：无_
  - _验收：`tests/test_render.py` 中新增 `test_manifest_default_code_sync`
    断言默认 `enabled=True, bucket="raytrain-code"`。_

- [x] 1.5 在 `raytrain/user_config.py` 中加入 `code_bucket: str = "raytrain-code"`
  字段；`UserConfig.load` / `save` 兼容新旧 yaml。
  - _依赖：无_
  - _验收：写入 / 读取 round-trip；缺失字段时按默认值填充。_

### 2. 模板与 Driver 改造

- [x] 2.1 修改 `raytrain/templates/rayjob.yaml.j2`，新增 `code_uri` /
  `code_hash` / `code_size_bytes` 块；当 `code_uri` 非空时写入 `working_dir`、
  `setup_timeout_seconds: 600`、`RAYTRAIN_CODE_*` env_vars。
  - _依赖：无_
  - _验收：扩展 `tests/test_render.py`，新增两个测试：
    `test_render_with_code_sync` 断言 YAML 中包含 `working_dir: s3://...`；
    `test_render_without_code_sync` 断言 YAML 中无 `working_dir` 字段。_

- [x] 2.2 修改 `raytrain/rayjob.py` 的 `Plan` dataclass + `RenderInputs`，
  新增 `code_uri` / `code_hash` / `code_size_bytes` 三个字段，并在 `render_rayjob`
  中传入模板上下文。
  - _依赖：2.1_
  - _验收：`tests/test_render.py` 通过；`Plan.to_yaml` 输出包含新字段。_

- [x] 2.3 修改 `raytrain/cli/submit.py`，加入 5 阶段进度输出 + `--workdir-zip`、
  `--no-code-sync`、`--code-bucket` 三个 CLI 选项；调用 `code_sync` 模块完成打包/
  上传；把结果填入 `Plan`。`--no-code-sync` 时跳过 [1/5][2/5] 并不写
  `working_dir`。
  - _依赖：1.2、1.3、2.2_
  - _验收：`raytrain submit --dry-run --config ... --no-code-sync` 输出 4 阶段
    且模板里无 `working_dir`；不带 `--no-code-sync` 时输出 5 阶段且模板里有
    `working_dir`。_

- [x] 2.4 修改 `raytrain/entrypoint/driver.py`，新增 `_resolve_workdir(plan,
  manifest)`：优先读 `RAY_RUNTIME_ENV_WORKING_DIR`，否则退到 manifest/plan 的
  `workdir`；driver 主流程与 NodeLauncher 都用此函数。
  - _依赖：2.3_
  - _验收：新增 `tests/test_driver_workdir.py`：
    - 设置环境变量时返回该路径；
    - 未设置时回退；
    - 都缺失时抛错。_

- [x] 2.5 把 `RAYTRAIN_CODE_HASH` / `RAYTRAIN_CODE_URI` 透传到子进程 env，并在
  driver 启动日志中输出 `code_hash` 前 12 位。
  - _依赖：2.4_
  - _验收：手工提交一次任务后，在 head pod 日志中找到 "[driver] code_hash=..."
    一行；MLflow run tag 包含 `raytrain.code_hash`。_

### 3. 部署与运维

- [x] 3.1 新增 `deploy/setup-code-bucket.sh`：用 `mc` 创建 `raytrain-code` bucket
  并写入 7 天 lifecycle policy；脚本幂等。
  - _依赖：无_
  - _验收：手工跑一次：`mc ls raytrain` 看到 `raytrain-code/`；
    `mc ilm export raytrain/raytrain-code` 输出包含 `Days: 7`。_

- [x] 3.2 在 `docs/ops-guide.md` 中追加"Code Bucket 运维"小节，说明 bucket 名、
  lifecycle、配额建议、紧急清理操作。
  - _依赖：3.1_
  - _验收：md 通过 `markdownlint`；同事 review 一遍。_

### 4. 用户文档与回归

- [x] 4.1 更新 `docs/quickstart.md`：在第 5 步 `raytrain submit` 后加一句
  "默认会打包当前目录代码并上传 MinIO，无需 build 镜像；如要回退用
  `--no-code-sync`"。
  - _依赖：2.3_

- [x] 4.2 更新 `docs/user-guide.md` 增加 "代码同步与镜像" 一节，说明：
  - working_dir 排除规则
  - `.raytrainignore` 用法
  - 200MiB 上限
  - Code_Hash 与 MLflow tag
  - Code_Zip 7 天保留
  - 镜像现在只放环境，不放代码
  - _依赖：2.3、3.2_

- [x] 4.3 在 `docs/adding-new-repo.md` 中说明：新仓库如何写 `.raytrainignore`、
  如何选择保留在镜像里的依赖。
  - _依赖：4.2_

- [x] 4.4 跑全量 `make test` + 端到端 smoke：在 dev namespace 用 working_dir
  模式跑 pointcept 1×1 GPU 任务到第一个 step 输出 loss。
  - _依赖：以上全部 1.x、2.x_
  - _验收：训练日志出现 `loss=`，MLflow run 中 `raytrain.code_uri` 与
    `raytrain.code_hash` 已写入。_

- [x] 4.5 手工验证 `--no-code-sync` 回退路径：用旧版镜像 + `--no-code-sync`
  跑通同一个 smoke run，确认与 Phase 1 改造前行为一致。
  - _依赖：4.4_

### 5. Phase 1 收口

- [x] 5.1 在 1 个 namespace 灰度 3 天，收集 zip 大小分布、解压失败率、用户反馈；
  整理到 `docs/phase1-rollout.md`。
  - _依赖：4.4、4.5_

- [x] 5.2 全量推开 working_dir 默认值；release notes 注明默认行为变更与回退方法。
  - _依赖：5.1_

---

## Phase 2 · 长寿 RayCluster + 凭据收敛

### 6. 长寿 RayCluster 部署

- [x] 6.1 创建 `deploy/shared-cluster/raycluster-h20.yaml`：head + workerGroup
  (h20)；`enableInTreeAutoscaling: true`，`minReplicas: 0`，labels
  `raytrain.shared=true`、`raytrain.gpu_type=h20`；附带 ClusterIP Service。
  - _依赖：无_
  - _验收：`kubectl apply -f` 后 head pod Running；`kubectl get raycluster
    -l raytrain.shared=true` 能看到。_

- [x] 6.2 创建 `deploy/shared-cluster/raycluster-a100.yaml`，同上但 `gpu_type=a100`。
  - _依赖：6.1_

- [x] 6.3 创建 `deploy/shared-cluster/README.md`，说明部署/升级/排障流程；明确
  Ray 版本升级时的 drain 步骤。
  - _依赖：6.1、6.2_

### 7. Submission Server

- [x] 7.1 新建 `raytrain/server/` 包；最小 FastAPI 应用，提供 `/healthz`、`/readyz`。
  - _依赖：无_
  - _验收：`uvicorn raytrain.server.app:app` 本地起得来；`curl /healthz` 200。_

- [x] 7.2 在 `raytrain/server/auth.py` 实现 `verify_token(req) -> Identity`，支持
  raytrain 自签 JWT（HS256，secret 来自 env `RAYTRAIN_JWT_SECRET`）+ OIDC ID
  Token（issuer/jwks 来自 env）。
  - _依赖：7.1_
  - _验收：`tests/test_server_auth.py` 全部通过（合法 / 过期 / 错签 / OIDC mock）。_

- [x] 7.3 在 `raytrain/server/ray_client.py` 包装 `JobSubmissionClient`：根据
  `gpu_type` 取 cluster URL；提供 `submit_job` / `stop_job` / `tail_logs` 三个
  方法，单元用 mock 验证。
  - _依赖：7.1_
  - _验收：`tests/test_ray_client.py` 通过。_

- [x] 7.4 实现 `POST /v1/jobs`：组装 `runtime_env`，调用 `ray_client.submit_job`，
  返回 `submission_id`；写审计日志。
  - _依赖：7.2、7.3_
  - _验收：`tests/test_server_submit.py` 验证 happy-path + token 拒绝 + 5xx 上游
    重试 3 次。_

- [x] 7.5 实现 `GET /v1/jobs/{id}/logs` SSE 流接口；`DELETE /v1/jobs/{id}`；
  `GET /v1/jobs?owner=`。
  - _依赖：7.4_
  - _验收：用 `httpx` mock 模拟 Ray 端流式日志，断言行序与超时正确。_

- [x] 7.6 编写 `deploy/server/Dockerfile` + `deploy/server/deployment.yaml`，
  把 server 部署到 `raytrain-system` namespace；Service ClusterIP 暴露 8080；
  Ingress / NodePort 二选一暴露 HTTPS。
  - _依赖：7.5_
  - _验收：dev 集群部署后 `curl https://.../healthz` 200。_

### 8. CLI 适配 cluster_mode

- [x] 8.1 在 `raytrain/user_config.py` 增加 `submission_server`、`token`、
  `shared_clusters`、`default_cluster_mode` 字段；`raytrain configure` 增加对应
  prompt（向后兼容：旧版 yaml 缺字段时取默认）。
  - _依赖：无_

- [x] 8.2 在 `raytrain/cli/submit.py` 增加 `--cluster-mode {per_job|shared}`
  flag；解析顺序 CLI > namespace ConfigMap (`raytrain-defaults`) > User_Config。
  - _依赖：8.1_
  - _验收：`tests/test_cli_cluster_mode.py::test_priority_order` 通过。_

- [x] 8.3 实现 shared 模式提交分支：构建 Code_Zip → 上传 → 调
  `POST /v1/jobs`；从响应里取 `submission_id`；输出与 per_job 模式一致的 4 阶段
  进度。
  - _依赖：7.4、8.2_
  - _验收：mock httpx 后 `tests/test_cli_cluster_mode.py::test_shared_submit`
    通过；不调用任何 K8s API。_

- [x] 8.4 修改 `raytrain logs` / `raytrain stop` / `raytrain list`，按
  `cluster_mode` 调用对应后端；`list` 合并展示两类来源，前缀
  `[per-job] / [shared]`。
  - _依赖：7.5、8.3_
  - _验收：mock 测试 + dev 集群手工跑 1 个 shared + 1 个 per_job，`raytrain list`
    同时显示。_

- [x] 8.5 修改 driver，让它可以从 `RAYTRAIN_MANIFEST_B64` / `RAYTRAIN_PLAN_B64`
  反序列化 manifest/plan（fallback：旧的 `--manifest`/`--plan` 文件路径）。
  - _依赖：无（与 Phase 1 driver 平行）_
  - _验收：`tests/test_driver_envload.py` 通过。_

### 9. Token 颁发与多租户

- [x] 9.1 编写 `deploy/issue-token.sh <user> [--days 30] [--tenant <id>]`，从 K8s
  Secret `raytrain-jwt-key` 读 HS256 secret 并签发 JWT；输出到 stdout 与文件
  `token-<user>.txt`（0600）。
  - _依赖：7.2_

- [x] 9.2 在 `raytrain configure` 中引导用户填 `submission_server` + `token`；
  本地不再要求 kubeconfig（仅在 `cluster_mode=per_job` 时检查）。
  - _依赖：8.1_
  - _验收：`raytrain configure` 已 prompt `--cluster-mode` / `--submission-server`
    / `--token` 并写入 UserConfig；新增 mode 相关提示（shared 缺 server/token 时
    告警，per_job 时提示需要 kubeconfig）。kubeconfig 仅在 per_job 提交路径
    （submit 第 6 步 `load_kube()`）加载，shared 模式经 `_submit_shared` 提前返回
    不触碰 K8s；中间层 `_configmap_cluster_mode` 为 best-effort，CLI 指定模式时
    短路不连 kube。`tests/test_configure.py`（3 个用例，含 load_kube fail-if-called
    守卫）+ `tests/test_cli_cluster_mode.py::test_shared_submit` 通过。_

- [x] 9.3 实现 `RAYTRAIN_TENANT` 注入与 server 端 `tenant_isolation: strict`
  检查；`/v1/jobs/{id}/logs` 拒绝跨 tenant 访问。
  - _依赖：7.4、7.5_
  - _验收：`tests/test_server_tenant.py` 通过。_

### 10. 迁移路径与回退

- [x] 10.1 编写 `deploy/set-default-cluster-mode.sh <per_job|shared>`：写
  namespace ConfigMap `raytrain-defaults`。
  - _依赖：8.2_

- [x] 10.2 编写 `docs/migration-shared-cluster.md`，覆盖：迁移步骤、不兼容点、
  回退方法、FAQ。
  - _依赖：8.x、9.x_

- [x] 10.3 在 5 名白名单用户上灰度 1–2 周（`default_cluster_mode=shared`），
  每天检查：submission 成功率、worker 启动时延、autoscaling 行为、log 流稳定性；
  汇总到 `docs/phase2-rollout.md`。
  - _依赖：8.4、9.2_

- [x] 10.4 全量切换 `default_cluster_mode=shared`；保持 `--cluster-mode per_job`
  作为应急回退。
  - _依赖：10.3_

- [x] 10.5 1 个月稳定后，停止给新用户下发 K8s kubeconfig，所有新用户走 token；
  旧 kubeconfig 按到期自然失效。
  - _依赖：10.4_

- [x] 10.6 6 个月后把 `--cluster-mode per_job` 标记 deprecated（CLI warning），
  再 6 个月删除该路径与对应 K8s 提交代码。
  - _依赖：10.5_

---

## 公共横切

- [x] 11.1 实现 `raytrain reproduce <mlflow_run_id>` 子命令：从 MLflow tag 读
  `code_uri`，下载到 `/tmp/raytrain-reproduce-<hash>/`；对象已被 lifecycle 删
  时友好报错。
  - _依赖：1.3_
  - _验收：`tests/test_reproduce.py` 通过；手工对一个 7 天内的 run + 一个
    7 天前的 run 各跑一次。_

- [x] 11.2 在 `raytrain submit` 完成后，把 manifest/plan 也作为 MLflow artifact
  上传（`/raytrain/manifest.yaml`、`/raytrain/plan.yaml`），便于审计。
  - _依赖：2.3_

- [x] 11.3 增加 nice-to-have：`code_sync.dedup: true` 时按 sha256 跨 user 复用
  blob（HEAD 命中即跳过 PUT）。
  - _依赖：1.3_
  - _验收：`tests/test_code_sync.py::test_dedup_skip_upload_when_blob_exists`
    通过。_
