# Requirements Document

## Introduction

raytrain 是一个把训练任务提交到 KubeRay 集群的 CLI/工具链。当前形态对用户有两个显著痛点：

1. 改一行训练代码就要 `docker build` + `docker push` 一次镜像，再提交。
2. 每个用户持有一份 K8s kubeconfig，泄露面广、审计弱、撤销难。

本特性定义 raytrain 的长期演进路径，分两阶段落地：

- **Phase 1（必须）：代码免镜像同步**。利用 Ray 自带的 `runtime_env.working_dir`，
  `raytrain submit` 时把当前工作目录打包上传 MinIO，集群侧 Ray 自动拉取并 chdir。
  镜像只在依赖（Python 包、CUDA、系统库）变更时才重 build。
- **Phase 2（必须，但可分批落地）：凭据收敛 + 长寿 RayCluster**。运维统一管理
  长寿 RayCluster；用户不再持有 K8s kubeconfig，改走 raytrain 服务端入口
  + Ray Job Submission API token / 公司 SSO；提交模式从 per-job RayCluster 切到
  shared cluster。

终态：用户在自己的代码目录里执行一条 `raytrain submit ...`，剩下的（打包、上传、
调度、日志、清理）由 raytrain 全部代劳；本地不需要 Docker，不需要 kubeconfig。

向后兼容是硬约束：现有 `.raytrain.yaml` + 现有镜像、现有 per-job cluster 提交
方式，在 Phase 1 必须继续可用；Phase 2 切 shared cluster 时通过开关 / 命名空间
级灰度，避免一次性 breaking change。

## Glossary

- **raytrain CLI**: 用户机器上的命令行工具，目前对应 `raytrain/cli/`。
- **raytrain Driver**: head pod 内 `raytrain.entrypoint.driver` 模块，负责申请
  placement group、起 NodeLauncher、跑训练子进程、上传 artifact。
- **RayJob**: KubeRay 自定义资源；当前每次 `raytrain submit` 创建一个 RayJob，
  RayJob 拉起一个 per-job RayCluster，跑完即销毁（`shutdownAfterJobFinishes: true`）。
- **RayCluster**: KubeRay 自定义资源，描述一个 head + worker pod 池。
- **Per_Job_Cluster_Mode**: 当前提交模式，每个 RayJob 自带一个 RayCluster（终生
  与 RayJob 等同）。
- **Shared_Cluster_Mode**: 终态提交模式，运维预先创建长寿 RayCluster，用户提交时
  通过 Ray Job Submission API 把任务投递到目标 cluster，cluster 不随任务消亡。
- **Code_Zip**: `raytrain submit` 在客户端打包的当前工作目录 zip 文件，承载用户
  当前代码（不含数据、不含 .git、不含 venv 等）。
- **Code_Bucket**: MinIO 上专门存放 Code_Zip 的 bucket，例如
  `s3://raytrain-code/<user>/<job_name>.zip`，配 7 天 lifecycle。
- **Working_Dir_Mechanism**: Ray `runtime_env.working_dir` 机制；Ray 自动在每个
  worker 上把指定 zip/目录解压到一个临时路径并 chdir。
- **Manifest**: 仓库根的 `.raytrain.yaml`，schema 见 `raytrain/manifest.py`。
- **User_Config**: 用户机器上的 `~/.raytrain/config.yaml`，存 MinIO/MLflow 凭据
  和 K8s namespace。
- **Submission_Server**: Phase 2 引入的 raytrain 服务端进程；接受用户的
  `raytrain submit`，再以 server 身份调 K8s / Ray Job Submission API。
- **Raytrain_Token**: Phase 2 用户向 Submission_Server 出示的身份凭证；可由
  raytrain 自身签发，也可对接公司 SSO（OIDC）。
- **Ray_Job_Submission_API**: Ray 集群的 HTTP API（默认 dashboard 8265），用
  `ray job submit` 风格往一个已有 RayCluster 投递任务，不需要 K8s 权限。
- **Code_Hash**: Code_Zip 内容的 SHA256，用于复用同内容的 zip、做版本审计。

## Requirements

---

### Phase 1 · 代码免镜像同步（must-have）

---

### Requirement 1 · 客户端打包与上传 working_dir

**User Story:** 作为训练用户，我希望执行 `raytrain submit` 时，raytrain 自动把
当前目录的代码打包上传到集群可读的对象存储，这样我改完代码不用再 build 镜像。

#### Acceptance Criteria

1. WHEN 用户在仓库根执行 `raytrain submit`，THE raytrain_CLI SHALL 把当前工作目录
   打包成单个 zip 文件，命名为 `raytrain-code-{job_name}.zip`，并存放于一个可清理
   的临时目录。
2. WHEN raytrain_CLI 打包工作目录，THE raytrain_CLI SHALL 排除以下默认条目：
   `.git/`、`.venv/`、`venv/`、`__pycache__/`、`*.pyc`、`.pytest_cache/`、
   `.mypy_cache/`、`.ruff_cache/`、`.idea/`、`.vscode/`、`node_modules/`、
   `data/`、`datasets/`、`exp/`、`logs/`、`*.ckpt`、`*.pth`、`*.pt`、`*.tar`、
   `*.zip`、以及仓库根 `.gitignore` 中列出的条目。
3. THE raytrain_CLI SHALL 同时读取仓库根的 `.raytrainignore`（如存在），把其中
   每行作为 gitignore 风格 pattern 追加到排除规则中。
4. WHEN 打包后的 zip 文件大小超过 `200 MiB`，THE raytrain_CLI SHALL 终止提交
   流程，打印当前 zip 大小、最大允许大小、以及 zip 中体积排名前 10 的文件路径
   及大小，并以非零退出码返回。
5. THE raytrain_CLI SHALL 计算 Code_Zip 的 SHA256 哈希作为 Code_Hash，并在终端
   输出该哈希的前 12 位。
6. WHEN raytrain_CLI 准备上传 Code_Zip，THE raytrain_CLI SHALL 使用 User_Config
   中的 MinIO 凭据，把 zip 上传到 `s3://{code_bucket}/{user}/{job_name}.zip`，
   其中 `code_bucket` 默认为 `raytrain-code`，可由 User_Config 中的
   `code_bucket` 字段覆盖。
7. WHEN MinIO 上传过程中网络中断或返回 5xx，THE raytrain_CLI SHALL 最多自动重试
   3 次，每次退避 `2s / 4s / 8s`；3 次后仍失败则以非零退出码终止并打印 MinIO 端
   返回的错误码与消息。
8. WHEN 上传完成，THE raytrain_CLI SHALL 在终端输出该 zip 的 s3 URI、大小（MiB）、
   Code_Hash 前 12 位、以及预计的过期时间（now + 7d）。
9. THE raytrain_CLI SHALL 提供 `--workdir-zip <path>` 选项；WHEN 该选项被指定，
   THE raytrain_CLI SHALL 直接使用该路径下的 zip 文件而不再重新打包，但仍会校验
   zip 大小并计算 Code_Hash。
10. THE raytrain_CLI SHALL 提供 `--no-code-sync` 开关；WHEN 该开关被指定，
    THE raytrain_CLI SHALL 跳过打包和上传步骤，并按"镜像内代码"模式提交（与
    当前行为完全一致），用于回滚或排查 working_dir 故障。

---

### Requirement 2 · RayJob 模板注入 working_dir

**User Story:** 作为 raytrain 框架开发者，我希望 RayJob 启动时通过
`runtime_env.working_dir` 自动从 MinIO 拉取代码 zip，这样训练代码不再需要打到镜像里。

#### Acceptance Criteria

1. WHEN raytrain 渲染 RayJob 模板且 Code_Zip 已上传，THE Rayjob_Renderer SHALL
   在 RayJob `spec.runtimeEnvYAML` 中写入 `working_dir: <s3_uri>`，其中 `<s3_uri>`
   是该 zip 的 `s3://{code_bucket}/{user}/{job_name}.zip`。
2. WHEN raytrain 渲染 RayJob 模板，THE Rayjob_Renderer SHALL 在
   `runtimeEnvYAML.env_vars` 中注入 `RAYTRAIN_CODE_HASH`、`RAYTRAIN_CODE_URI`、
   `RAYTRAIN_CODE_SIZE_BYTES` 三个环境变量。
3. WHEN raytrain 渲染 RayJob 模板，THE Rayjob_Renderer SHALL 在
   `runtimeEnvYAML.config` 中设置 `setup_timeout_seconds: 600`，避免 worker
   首次拉 zip 超时。
4. WHEN Code_Zip 由用户在客户端通过 `--no-code-sync` 关闭，THE Rayjob_Renderer
   SHALL 不在 `runtimeEnvYAML` 中写 `working_dir` 字段，保持与当前模板行为一致。
5. THE Rayjob_Renderer SHALL 在 `working_dir` 之外，仍保留现有
   `RAYTRAIN_USER` / `RAYTRAIN_JOB_NAME` / `RAYTRAIN_REPO` / `PYTHONUNBUFFERED`
   等 env_vars，避免 Phase 1 引入对环境的 breaking change。
6. WHEN Manifest 中 `workdir` 字段与 `runtime_env.working_dir` 同时存在，
   THE Rayjob_Renderer SHALL 把 `workdir` 解释为"运行子进程时的相对 cwd 起点"，
   并把该 cwd 在 driver 中转换为 working_dir 解压后的路径（详见 Requirement 3）。

---

### Requirement 3 · Driver 在 working_dir 模式下定位代码

**User Story:** 作为 raytrain Driver，我希望在 working_dir 模式下能正确找到训练
入口脚本和配置文件，这样训练命令的相对路径（如 `tools/train.py` / `configs/...`）
保持与原仓库一致。

#### Acceptance Criteria

1. WHEN raytrain_Driver 启动且环境变量 `RAY_RUNTIME_ENV_WORKING_DIR` 非空，
   THE raytrain_Driver SHALL 把该路径作为子进程的 `cwd`，覆盖 Manifest 中的
   `workdir` 字段。
2. IF 环境变量 `RAY_RUNTIME_ENV_WORKING_DIR` 为空且 Manifest 中 `workdir` 也为空，
   THEN THE raytrain_Driver SHALL 终止训练并输出错误信息，要求用户在 Manifest
   中显式设置 `workdir` 或启用 working_dir 同步。
3. WHEN raytrain_Driver 在 working_dir 模式下启动 NodeLauncher actor，
   THE raytrain_Driver SHALL 把 NodeLauncher 的工作目录设置为 working_dir 解压
   后的路径，使各 worker 的子进程在相同相对路径下运行。
4. WHEN Manifest 中 `launcher.entrypoint` 指向一个 working_dir 内的相对路径
   （如 `tools/train.py`），THE raytrain_Driver SHALL 不再做任何路径前缀拼接，
   交由 Ray 的 working_dir 解压逻辑解析。
5. THE raytrain_Driver SHALL 把 `RAYTRAIN_CODE_HASH` 透传到训练子进程，便于训练
   代码把 code hash 落到 MLflow tag / checkpoint 元数据中（用于复现）。

---

### Requirement 4 · Code_Bucket 生命周期与配额

**User Story:** 作为运维，我希望 Code_Zip 不会无限增长占满 MinIO，也希望任何
历史提交在 7 天内可复现。

#### Acceptance Criteria

1. THE deploy 脚本 SHALL 提供一份 `setup-code-bucket.sh`；WHEN 管理员执行该脚本，
   THE 脚本 SHALL 在 MinIO 上创建 `raytrain-code` bucket（已存在则跳过），并
   通过 `mc ilm` 写入"对象创建后 7 天自动删除"的 lifecycle policy。
2. THE setup-code-bucket.sh SHALL 在脚本注释中明确说明：lifecycle 保留期为 7 天，
   超过该窗口的 RayJob 即使重提交也无法恢复 code，必须重新打包。
3. THE raytrain_CLI SHALL 在 `MLflow run tags` 中写入 `raytrain.code_uri`、
   `raytrain.code_hash`、`raytrain.code_size_bytes` 三个键，用于事后审计与复现。
4. WHILE 同一 user 在 1 分钟内重复提交相同 Code_Hash 的 zip，THE raytrain_CLI
   SHALL 跳过实际上传步骤，直接复用上一次的 s3 URI（用 HEAD 请求确认对象存在
   且 size 一致）。

---

### Requirement 5 · Phase 1 向后兼容

**User Story:** 作为存量用户，我希望升级到带有 working_dir 能力的新版 raytrain
后，原有的 `.raytrain.yaml` + 镜像化代码工作流继续可用。

#### Acceptance Criteria

1. WHEN raytrain_CLI 检测到 Manifest 中存在新增字段 `code_sync.enabled: false`，
   THE raytrain_CLI SHALL 使用旧的镜像内代码模式提交，不打包 working_dir。
2. WHEN raytrain_CLI 检测到 Manifest 中不存在 `code_sync` 字段，
   THE raytrain_CLI SHALL 默认启用 working_dir 模式（即 `enabled: true`）；
   该默认值的变更必须在 release notes 中明确标记。
3. WHEN 用户使用旧版本镜像（不包含 raytrain 新版 driver）但启用了 working_dir，
   THE raytrain_Driver SHALL 在启动时检测 driver 版本，IF driver 不识别
   `RAYTRAIN_CODE_HASH`，THEN THE raytrain_Driver SHALL 输出可读的错误信息要求
   用户重 build 镜像或暂时使用 `--no-code-sync` 回退。
4. THE existing tests under `tests/test_render.py` SHALL 在 Phase 1 改造完成后
   继续通过；任何已有测试用例在迁移过程中只允许补充断言，不允许删除断言。

---

### Phase 2 · 长寿 RayCluster + 凭据收敛（must-have，分阶段）

---

### Requirement 6 · 长寿 RayCluster 部署清单

**User Story:** 作为运维，我希望以声明式方式部署"按 GPU 类型分组的长寿
RayCluster"，并在升级 Ray / KubeRay 版本时不必手动重建。

#### Acceptance Criteria

1. THE deploy 仓库 SHALL 在 `deploy/shared-cluster/` 目录下提供 RayCluster CR
   清单，至少包括：`raycluster-h20.yaml`、`raycluster-a100.yaml`，每份对应一个
   独立的 GPU pool。
2. THE shared-cluster RayCluster SHALL 启用 `enableInTreeAutoscaling: true`，
   并把 worker `minReplicas: 0`，避免空闲时占用 GPU。
3. THE shared-cluster RayCluster SHALL 暴露一个 ClusterIP Service
   `ray-shared-{gpu_type}-head`，端口 `8265`（dashboard / Job Submission API）、
   `10001`（client）、`6379`（gcs）。
4. WHEN 管理员执行 `kubectl apply -f deploy/shared-cluster/`，THE 部署清单 SHALL
   幂等地创建/更新 RayCluster，且不删除已有 RayJob。
5. THE shared-cluster RayCluster SHALL 给 head/worker 加上 label
   `raytrain.shared: "true"` 和 `raytrain.gpu_type: <h20|a100>`，便于 raytrain
   在 list 时把 shared cluster 与 per-job cluster 区分开。

---

### Requirement 7 · 客户端切到 Ray Job Submission API

**User Story:** 作为训练用户，我希望提交命令保持 `raytrain submit ...` 不变，
但底层不再创建 per-job RayCluster，而是把任务投递到运维管的长寿 RayCluster。

#### Acceptance Criteria

1. THE raytrain_CLI SHALL 支持 Manifest / User_Config 中新增字段
   `cluster_mode: per_job | shared`，默认值由 User_Config 控制；首发版本默认值
   保持 `per_job`（与现状一致），灰度切到 `shared`。
2. WHEN `cluster_mode: shared` 且 `--gpu-type` 已指定，THE raytrain_CLI SHALL
   解析对应的长寿 RayCluster head 地址（默认从 User_Config 字段
   `shared_clusters: {h20: <url>, a100: <url>}` 读取），并通过 Ray Job
   Submission API HTTP 接口提交任务，而不再调 K8s 创建 RayJob。
3. WHEN 通过 Ray Job Submission API 提交，THE raytrain_CLI SHALL 在
   `submission_id` 字段写入 `<user>-<repo>-<exp>-<stamp>` 格式的字符串，并把
   该字符串作为后续 `raytrain logs` / `raytrain stop` 的引用键。
4. WHEN `cluster_mode: shared`，THE raytrain_CLI SHALL 把 Code_Zip 的 s3 URI
   作为 `runtime_env.working_dir` 传给 Ray Job Submission API；同时把 Manifest
   + Plan 序列化后通过 `runtime_env.env_vars`（base64 of yaml）注入，避免依赖
   K8s ConfigMap。
5. WHEN `cluster_mode: shared`，THE raytrain_CLI SHALL 不再创建 K8s ConfigMap /
   Secret / Service / RayJob 资源，整个提交流程中不调用 K8s API。
6. WHILE `cluster_mode: per_job`，THE raytrain_CLI SHALL 维持当前 K8s 提交流程
   不变，确保过渡期内两种模式可同时使用。

---

### Requirement 8 · 用户身份从 kubeconfig 迁移到 Raytrain_Token

**User Story:** 作为安全负责人，我希望用户不再持有 K8s kubeconfig；用户只持有
一份可短期撤销的 raytrain token，实际的 K8s 操作由集中服务代为执行。

#### Acceptance Criteria

1. THE raytrain Submission_Server SHALL 提供 HTTPS 接口 `POST /v1/jobs`，请求体
   包含 Code_Zip 的 s3 URI、Manifest（yaml）、Plan（yaml）、`gpu_type`、
   `num_nodes`、`gpus_per_node`、`mlflow_run_id` 等字段。
2. THE Submission_Server SHALL 校验请求 Header `Authorization: Bearer <token>`；
   IF token 不合法或已过期，THEN THE Submission_Server SHALL 返回 HTTP 401 并
   附带可读的错误消息（不泄露 token 比对细节）。
3. THE Submission_Server SHALL 支持两类 token 来源：
   (a) raytrain 自身签发的 JWT，secret 由 K8s Secret 管理；
   (b) 公司 SSO（OIDC）颁发的 ID Token，issuer / jwks_url 在 Submission_Server
   配置中声明。
4. WHEN 收到合法请求，THE Submission_Server SHALL 以自身 ServiceAccount 身份调
   Ray Job Submission API（shared cluster）或 K8s API（per_job cluster），用户
   不需要任何 K8s 凭据。
5. THE Submission_Server SHALL 把 `submission_id` 与提交者 token 的 `sub`（用户
   标识）写入审计日志，单条日志包含：时间、user、gpu_type、num_nodes、
   gpus_per_node、code_uri、code_hash、submission_id、result。
6. WHEN raytrain_CLI 在 User_Config 中检测到 `submission_server: <url>` 字段，
   THE raytrain_CLI SHALL 使用该字段指定的 server 进行提交，并不再要求本地存在
   kubeconfig；ELSE THE raytrain_CLI SHALL 回退到当前的本地 K8s 直连模式。
7. THE Submission_Server SHALL 提供 `GET /v1/jobs/{submission_id}/logs` 接口，
   以 `text/event-stream` 格式增量推送日志，raytrain_CLI 据此实现 `raytrain logs -f`。
8. THE Submission_Server SHALL 提供 `DELETE /v1/jobs/{submission_id}` 接口，用于
   `raytrain stop`；删除时 server 调 Ray Job Submission API 的 `stop` 接口，并
   级联清理 per_job 模式下创建的 ConfigMap / Secret / Service。

---

### Requirement 9 · Phase 2 迁移路径与回退

**User Story:** 作为运维，我希望 per_job → shared 切换不是一次性 breaking change，
出问题可以单租户回退。

#### Acceptance Criteria

1. THE raytrain_CLI SHALL 支持单次提交时通过 `--cluster-mode {per_job|shared}`
   覆盖 User_Config 中的默认值。
2. WHEN 同一 namespace 同时存在 per_job RayJob 与 shared cluster 投递的任务，
   THE raytrain_CLI `list` 命令 SHALL 同时列出两类任务，并在第一列以
   `[per-job] / [shared]` 标注来源。
3. WHEN 管理员希望强制全员切到 shared，THE deploy 脚本 SHALL 提供
   `set-default-cluster-mode.sh shared`，把 namespace 级 ConfigMap
   `raytrain-defaults` 中的 `cluster_mode` 改为 `shared`；ConfigMap 优先级
   高于 User_Config，但低于 CLI 标志。
4. THE 文档 `docs/migration-shared-cluster.md` SHALL 列出 per_job → shared 的
   完整迁移步骤、已知不兼容点、以及回退到 per_job 的操作。

---

### Phase 1 / Phase 2 公共要求

---

### Requirement 10 · 用户体验对齐（must-have）

**User Story:** 作为训练用户，我希望最终交互就是一条 `raytrain submit ...`，
不用关心打包、上传、调度、日志这些底层细节。

#### Acceptance Criteria

1. THE raytrain_CLI `submit` 命令 SHALL 在 Phase 1 / Phase 2 改造后保持向后
   兼容的命令行 flag 集合：现有的 `--config`、`--name`、`--gpus`、`--nodes`、
   `--gpu-type`、`--image`、`--manifest-path`、`--experiment`、
   `--service-account`、`--ttl`、`--dry-run`、`--config-override`、`--save-path`
   含义和行为不得变化。
2. WHEN raytrain_CLI 进入 working_dir 模式，THE raytrain_CLI SHALL 在终端按
   编号顺序输出至少四个进度阶段（packaging code / uploading code /
   creating MLflow run / submitting job），每阶段成功后用 `[N/M]` 前缀提示。
3. THE raytrain_CLI SHALL 在所有错误路径中，保证终端打印的错误消息包含：
   失败阶段名称、错误码、最近一次的远端响应（如有）、以及"下一步建议"
   一段提示。

---

### Requirement 11 · 数据路径不打包到 Code_Zip（must-have）

**User Story:** 作为训练用户，我希望我配置在 `datasets:` / `data_source:` 中的
数据路径不会被打进 Code_Zip，避免 zip 体积膨胀。

#### Acceptance Criteria

1. WHEN raytrain_CLI 打包 Code_Zip，THE raytrain_CLI SHALL 把 Manifest 中
   `datasets[*].mount` 列出的相对路径加入排除规则。
2. WHEN raytrain_CLI 打包 Code_Zip，THE raytrain_CLI SHALL 把 Manifest 中
   `data_source.uri` 关联的本地缓存目录（默认 `data/`、`datasets/`）加入
   排除规则。
3. IF Code_Zip 中仍包含某个被 `datasets[*].mount` 标记的路径（用户硬塞），
   THEN THE raytrain_CLI SHALL 在终端输出 warning，提示该路径已被排除规则覆盖，
   且 Driver 在运行时会用 dataset_sync 的 symlink 覆盖掉打包时的目录。

---

### Requirement 12 · 复现性与审计（must-have）

**User Story:** 作为算法负责人，我希望任意一次过去 7 天内的训练都能精确还原代码。

#### Acceptance Criteria

1. THE raytrain_CLI SHALL 在每次提交后，把 `code_uri` / `code_hash` /
   `image` / `manifest_yaml` / `plan_yaml` 写入 MLflow run 的 tags 与 artifacts
   （`/raytrain/manifest.yaml` / `/raytrain/plan.yaml`）。
2. THE raytrain_CLI SHALL 提供 `raytrain reproduce <mlflow_run_id>` 子命令；
   WHEN 用户执行该命令，THE raytrain_CLI SHALL 从 MLflow run tags 读出 code_uri，
   从 MinIO 下载该 zip 到本地临时目录，并在终端打印 zip 路径与 Code_Hash。
3. IF code_uri 对应的 MinIO 对象已被 lifecycle 删除，THEN THE
   `raytrain reproduce` 子命令 SHALL 返回非零退出码并输出："Code_Zip 已超过 7 天
   保留期限，无法恢复；请改用 git commit 关联回溯"。

---

### Phase 1 / Phase 2 共有的 nice-to-have

---

### Requirement 13 · Code_Zip 增量上传（nice-to-have）

**User Story:** 作为大仓库的用户，我希望 zip 上传走"按内容寻址 + 跨 user 复用"，
让 100MiB 级别的代码在十几秒内提交完。

#### Acceptance Criteria

1. WHERE 启用了实验性配置 `code_sync.dedup: true`，THE raytrain_CLI SHALL 在
   上传前对 zip 取 SHA256，先 HEAD `s3://raytrain-code/_blobs/{sha256}.zip`；
   IF 对象已存在，THEN THE raytrain_CLI SHALL 跳过 PUT，并把 `working_dir` 指向
   该对象。
2. WHERE 启用了 `code_sync.dedup: true`，THE deploy 脚本 SHALL 把 `_blobs/`
   前缀的 lifecycle 配置成 7 天，与 per-user 前缀保持一致。

---

### Requirement 14 · 共享 cluster 多租户隔离（nice-to-have，但 Phase 2 上线前需明确）

**User Story:** 作为安全负责人，我希望同一长寿 cluster 上的多个租户互相看不到对方的代码、log、checkpoint。

#### Acceptance Criteria

1. WHEN Submission_Server 提交任务到 shared cluster，THE Submission_Server SHALL
   在 `runtime_env.env_vars` 中注入 `RAYTRAIN_TENANT=<tenant_id>`，以及当前用户
   独有的 MinIO scoped credentials（仅可读自己的 code_bucket / scratch_bucket）。
2. WHERE 启用 `tenant_isolation: strict`，THE Submission_Server SHALL 拒绝任何
   跨租户的 `raytrain logs` 请求；只有 token 对应 tenant_id 与目标 submission 的
   tenant_id 一致时才返回日志。

