# Design Document

## Overview

本设计文档拆解 raytrain 长期演进的两阶段技术方案：

- **Phase 1**：在保留现有 per-job RayCluster 提交链路的前提下，引入 `working_dir`
  代码同步机制，让用户改完代码不再需要 build/push 镜像。
- **Phase 2**：把"提交"从客户端直连 K8s 演进为客户端 → raytrain Submission_Server
  → Ray Job Submission API → 长寿 RayCluster；同时把用户身份从 K8s kubeconfig
  迁移到 raytrain token / 公司 SSO。

设计原则：

1. **小步快跑**：Phase 1 不动 Phase 2 的任何东西；Phase 2 上线时 Phase 1 已稳定运行。
2. **向后兼容**：每次改造都通过开关 / Manifest 字段灰度，存量用户感知最小。
3. **单一改动维度**：客户端、模板、driver、server 四层中，每个 Story 只动一层
   或两层，避免一发不可收。

---

## Architecture

### 当前架构（reference）

```
┌────────────────────┐
│ User Machine       │
│  raytrain CLI      │  ── kubeconfig (per-user) ──┐
│  + .raytrain.yaml  │                              ▼
│  + ~/.raytrain     │                       ┌──────────────────┐
└────────────────────┘                       │  K8s API Server  │
                                             └─────┬────────────┘
                                                   │ create RayJob (per-job)
                                                   ▼
                                             ┌──────────────────┐
                                             │ KubeRay Operator │
                                             └─────┬────────────┘
                                                   │ creates per-job RayCluster
                                                   ▼
                                            ┌──────────────────────┐
                                            │ head pod (driver)    │
                                            │ + worker pods        │
                                            │ image: pre-baked code│  ← 痛点
                                            └──────────────────────┘
```

痛点：
- 改一行 Python → 重 build 整个镜像（4–8GB），浪费时间与寄存空间。
- 用户机器持有 K8s kubeconfig（namespace-scoped 但仍有泄露面）。
- 每个 Job 起一个 RayCluster，pod 启动开销 1–2 分钟。

### 终态架构（reference）

```
┌────────────────────┐
│ User Machine       │
│  raytrain CLI      │── code_zip ──▶ MinIO (raytrain-code/{user}/{job}.zip)
│                    │
│                    │── HTTPS + Bearer Token ──▶ ┌─────────────────────────┐
└────────────────────┘                            │ raytrain Submission_Srv │
                                                  │  - JWT/SSO verify       │
                                                  │  - audit log            │
                                                  │  - K8s SA inside cluster│
                                                  └────────────┬────────────┘
                                                               │ Ray Job Submission API
                                                               ▼
                                                ┌──────────────────────────┐
                                                │ Long-Lived RayCluster    │
                                                │  (h20 / a100, 一组一份)  │
                                                │  enableInTreeAutoscaling │
                                                │  worker minReplicas=0    │
                                                └──────────────────────────┘
                                                               │ runtime_env.working_dir
                                                               ▼ pulls code_zip from MinIO
                                                ┌──────────────────────────┐
                                                │ Ephemeral Job Workers    │
                                                │  - chdir to working_dir  │
                                                │  - run training subproc  │
                                                └──────────────────────────┘
```

### Phase 1 中间态（落地后即可达到）

```
┌────────────────────┐
│ User Machine       │
│  raytrain CLI      │── code_zip ──▶ MinIO (raytrain-code/{user}/{job}.zip)
│  + kubeconfig      │
│                    │── kubectl ──▶ K8s API Server
└────────────────────┘                       │ create RayJob with
                                             │   runtimeEnvYAML.working_dir = s3://...
                                             ▼
                                  ┌──────────────────────────┐
                                  │ per-job RayCluster (旧)  │
                                  │  Ray 自动从 MinIO 拉 zip │
                                  │  解压到 /tmp/ray/...     │
                                  │  driver chdir            │
                                  │  跑训练                   │
                                  └──────────────────────────┘
```

只增加一个 MinIO bucket + 模板里加一行，对存量用户零干扰。

---

## Components and Interfaces

### Phase 1

#### Component 1.1 · raytrain_CLI 增加 `code_sync` 模块

新增模块 `raytrain/code_sync.py`，对外暴露：

```python
@dataclass
class CodeBundle:
    zip_path: Path              # 本地临时 zip 路径
    s3_uri: str                 # 上传后的 s3://raytrain-code/<user>/<job>.zip
    sha256: str                 # 16 进制
    size_bytes: int

def build_code_zip(
    workdir: Path,
    job_name: str,
    user: str,
    extra_excludes: list[str],
    max_size_bytes: int = 200 * 1024 * 1024,
) -> CodeBundle: ...

def upload_code_zip(
    bundle: CodeBundle,
    minio_client: Minio,
    bucket: str = "raytrain-code",
    dedup: bool = False,
) -> str:  # returns s3 uri actually used
    ...
```

**打包实现选择：标准库 `zipfile` + 内存中流式构建**。理由：

- 不需要额外依赖（`tarfile.gz` 比 zip 慢且不利于 Ray 检测格式）。
- Ray `working_dir` 直接接受 `.zip`。
- 200MiB 上限下，`zipfile` 的内存开销可控。

**排除规则实现**：基于 `pathspec` 库（`gitignore` 风格的 PathSpec）；
项目本身已经依赖 PyYAML、minio、kubernetes，再加 `pathspec` 无明显成本。
默认 pattern 集合内置在 `code_sync.DEFAULT_IGNORES`，与
`.raytrainignore` 合并后形成最终 PathSpec。

**Code_Hash 计算时机**：在 zip 写入完成后，对最终文件做一次 SHA256；
不在打包过程中"边算边打"，避免与 zip metadata 顺序耦合。

**重试**：`upload_code_zip` 用一个简单的 `for attempt in range(3)` 循环，
失败时 `time.sleep(2 ** attempt * 2)`；只对 5xx / 网络错误重试，4xx 直接抛出。

#### Component 1.2 · raytrain/cli/submit.py 改造

`submit.py` 中按下面顺序加入 5 个阶段：

```
[1/5] packaging code (workdir=...)
      excluded: .git/, data/, ...
      zip size: 87.3 MiB, sha256: a3f8...
[2/5] uploading code -> s3://raytrain-code/zhangsan/zhangsan-pointcept-...zip
[3/5] creating MLflow run in experiment='pointcept'
      MLflow run_id = ...
[4/5] rendering RayJob (1×8 GPUs on h20, data: 4 dataset mounts)
[5/5] applying to namespace=ray-cluster-3
```

`--no-code-sync` 时跳过 [1/5]、[2/5]，回到当前 4 阶段输出。

新增 CLI 选项：
- `--workdir-zip <path>`：直接使用现成 zip。
- `--no-code-sync`：禁用 working_dir 路径。
- `--code-bucket <name>`：覆盖默认 `raytrain-code`。

`Plan` dataclass 新增字段：

```python
@dataclass
class Plan:
    ...
    code_uri: str | None = None
    code_hash: str | None = None
    code_size_bytes: int = 0
```

#### Component 1.3 · `templates/rayjob.yaml.j2` 改造

只动 `runtimeEnvYAML` 与新增 env_vars。当前模板：

```yaml
runtimeEnvYAML: |
  env_vars:
    RAYTRAIN_USER: "{{ user }}"
    RAYTRAIN_JOB_NAME: "{{ job_name }}"
    RAYTRAIN_REPO: "{{ repo_name }}"
    PYTHONUNBUFFERED: "1"
```

Phase 1 后：

```yaml
runtimeEnvYAML: |
  {%- if code_uri %}
  working_dir: "{{ code_uri }}"
  config:
    setup_timeout_seconds: 600
  {%- endif %}
  env_vars:
    RAYTRAIN_USER: "{{ user }}"
    RAYTRAIN_JOB_NAME: "{{ job_name }}"
    RAYTRAIN_REPO: "{{ repo_name }}"
    PYTHONUNBUFFERED: "1"
    {%- if code_uri %}
    RAYTRAIN_CODE_URI: "{{ code_uri }}"
    RAYTRAIN_CODE_HASH: "{{ code_hash }}"
    RAYTRAIN_CODE_SIZE_BYTES: "{{ code_size_bytes }}"
    {%- endif %}
```

注意：现在 `entrypoint` 是

```bash
bash -c 'export PYTHONPATH=...; exec /opt/conda/bin/python -m raytrain.entrypoint.driver --manifest /raytrain/manifest.yaml --plan /raytrain/plan.yaml'
```

它读的是 `/raytrain/...`（ConfigMap），与 working_dir 解压路径无关，可以保留。
关键是 NodeLauncher 的子进程 `cwd` 必须改成 working_dir 解压路径，由 driver
在运行时读环境变量决定（见 1.4）。

#### Component 1.4 · `entrypoint/driver.py` 改造

driver 当前从 manifest/plan 读 `workdir`，并把它直接传给 NodeLauncher.run 的
`cwd`。working_dir 模式下，Ray 把 zip 解压到一个临时目录并设置：

- 环境变量 `RAY_RUNTIME_ENV_HOOK` / `RAY_JOB_RUNTIME_ENV_WORKING_DIR`（取决
  于 Ray 版本，2.54 设的是 `RAY_RUNTIME_ENV_WORKING_DIR`，路径可
  通过 `ray.runtime_context.get_runtime_context()` 获取，但稳妥起见用环境变量）。

driver 变更：

```python
def _resolve_workdir(plan: dict, manifest: dict) -> str:
    rt_dir = os.environ.get("RAY_RUNTIME_ENV_WORKING_DIR")
    if rt_dir:
        return rt_dir
    return plan.get("workdir") or manifest["workdir"]
```

NodeLauncher 内同样调用 `_resolve_workdir`；因为环境变量来自 Ray runtime_env，
worker 上的 actor 进程内同样能读到。

为了支持 Manifest 里 `launcher.entrypoint: tools/train.py` 这类相对路径，driver
传给子进程的 `cwd` 必须是真正解压后的目录。

#### Component 1.5 · MinIO bucket lifecycle 部署

新增脚本 `deploy/setup-code-bucket.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
ENDPOINT="${MINIO_ENDPOINT:-http://172.31.16.3:30950}"
ACCESS_KEY="${MINIO_ACCESS_KEY:?must set}"
SECRET_KEY="${MINIO_SECRET_KEY:?must set}"
BUCKET="${1:-raytrain-code}"

mc alias set raytrain "$ENDPOINT" "$ACCESS_KEY" "$SECRET_KEY"
mc mb -p "raytrain/$BUCKET" || true
cat > /tmp/lifecycle.json <<JSON
{"Rules":[{"ID":"expire-7d","Status":"Enabled","Expiration":{"Days":7},"Filter":{}}]}
JSON
mc ilm import "raytrain/$BUCKET" < /tmp/lifecycle.json
echo "ok: bucket=$BUCKET, lifecycle=7d"
```

#### Component 1.6 · `manifest.py` schema 扩展

```python
@dataclass
class CodeSync:
    enabled: bool = True
    bucket: str = "raytrain-code"
    extra_excludes: list[str] = field(default_factory=list)
    dedup: bool = False              # nice-to-have

@dataclass
class Manifest:
    ...
    code_sync: CodeSync = field(default_factory=CodeSync)
```

`Manifest.load` 中：raw 没有 `code_sync` 字段时使用默认值（`enabled=True`）。

### Phase 2

#### Component 2.1 · 长寿 RayCluster YAML

`deploy/shared-cluster/raycluster-h20.yaml` 关键字段：

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: ray-shared-h20
  namespace: ray-shared
  labels:
    raytrain.shared: "true"
    raytrain.gpu_type: h20
spec:
  rayVersion: "2.54.1"
  enableInTreeAutoscaling: true
  headGroupSpec:
    rayStartParams:
      dashboard-host: "0.0.0.0"
      num-gpus: "0"
    template:
      spec:
        containers:
          - name: ray-head
            image: <env-only-image>     # 不含训练代码
            ...
  workerGroupSpecs:
    - groupName: gpu-workers
      replicas: 0
      minReplicas: 0
      maxReplicas: 16
      rayStartParams:
        num-gpus: "8"
      template:
        spec:
          nodeSelector: {gpu: "h20"}
          containers:
            - name: ray-worker
              image: <env-only-image>
              resources:
                limits: {nvidia.com/gpu: 8, cpu: "32", memory: "256Gi"}
              ...
```

附 `Service ray-shared-h20-head`（ClusterIP），暴露 8265 / 10001 / 6379。

#### Component 2.2 · raytrain Submission_Server

新模块 `raytrain/server/`，独立的 FastAPI 应用：

```
raytrain/server/
  __init__.py
  app.py            # FastAPI app, routes
  auth.py           # JWT/OIDC 验证
  audit.py          # 审计日志（structlog -> stdout / file）
  ray_client.py     # 包装 ray.job_submission.JobSubmissionClient
  config.py         # ServerConfig (env-driven: cluster urls, jwks, etc.)
```

关键路由：

| Method | Path                              | 说明                               |
|--------|-----------------------------------|------------------------------------|
| POST   | `/v1/jobs`                        | 提交一个新任务                     |
| GET    | `/v1/jobs/{submission_id}`        | 查状态                             |
| GET    | `/v1/jobs/{submission_id}/logs`   | SSE 流式日志                       |
| DELETE | `/v1/jobs/{submission_id}`        | 停止任务                           |
| GET    | `/v1/jobs?owner=...`              | 查列表（默认筛当前 token sub）     |
| GET    | `/healthz` / `/readyz`            | 探针                               |

`POST /v1/jobs` 请求体 schema：

```json
{
  "gpu_type": "h20",
  "num_nodes": 1,
  "gpus_per_node": 8,
  "code_uri": "s3://raytrain-code/zhangsan/...",
  "code_hash": "a3f8...",
  "manifest_yaml": "<base64-or-string>",
  "plan_yaml": "<base64-or-string>",
  "mlflow_run_id": "abc...",
  "runtime_env_extras": {}
}
```

server 内部把这些拼成 `runtime_env`：

```python
runtime_env = {
    "working_dir": req.code_uri,
    "env_vars": {
        "RAYTRAIN_RUN_ID": req.mlflow_run_id,
        "RAYTRAIN_CODE_HASH": req.code_hash,
        "RAYTRAIN_USER": token_sub,
        "RAYTRAIN_TENANT": tenant_id,
        "RAYTRAIN_MANIFEST_B64": base64(manifest_yaml),
        "RAYTRAIN_PLAN_B64": base64(plan_yaml),
        ...
    },
    "config": {"setup_timeout_seconds": 600},
}
client = JobSubmissionClient(SHARED_CLUSTER_URLS[req.gpu_type])
sub_id = client.submit_job(
    entrypoint="python -m raytrain.entrypoint.driver --from-env",
    runtime_env=runtime_env,
    submission_id=f"{token_sub}-{plan.repo_name}-{plan.exp_name}-{stamp}",
)
```

Driver 启动时改成"如果 `--manifest`/`--plan` 不存在，则从环境变量
`RAYTRAIN_MANIFEST_B64`/`RAYTRAIN_PLAN_B64` 反序列化"，这是 Phase 2 driver 唯一
新增的代码路径。Driver 主体逻辑（NodeLauncher、placement group、artifact 上传）
完全复用 Phase 1。

#### Component 2.3 · CLI `cluster_mode` 分支

`submit.py` 在 plan 渲染前：

```python
mode = cluster_mode_from(cli_flag, namespace_default_cm, user_config)
if mode == "shared":
    bundle = build_code_zip(...)            # 与 Phase 1 共用
    upload_code_zip(bundle, ...)            # 与 Phase 1 共用
    resp = httpx.post(
        f"{user_cfg.submission_server}/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        json=request_body,
    )
    submission_id = resp.json()["submission_id"]
else:
    # ...原 K8s 流程
```

`raytrain logs` / `raytrain stop` / `raytrain list` 同样按 `cluster_mode` 分支：
shared 模式调 server 的 REST API；per_job 模式继续用 K8s API。`list` 把两类来源
合并展示。

#### Component 2.4 · Token 颁发

对 raytrain 自签 JWT 的最小实现：

- 一个一次性脚本 `deploy/issue-token.sh <user> [--days 30]`，使用 K8s Secret
  `raytrain-jwt-key` 中的 HMAC secret 签发 JWT，包含 `sub`/`tenant`/`exp`/`iat`。
- 用户拿到 token 后写入 `~/.raytrain/config.yaml` 的 `token` 字段，CLI 透明加载。

对接公司 SSO（OIDC）由 server 配置 `oidc_issuer` / `oidc_audience` / `jwks_url`，
验证 ID Token 即可。两类 token 都进 `auth.verify_token(req)` 单一入口。

---

## Data Models

### Code_Zip 内部布局

```
job-name.zip
├── pyproject.toml
├── tools/
│   └── train.py
├── pointcept/
│   └── ...
├── configs/
│   └── ...
└── .raytrain.yaml
```

排除：`.git/`、`data/`、`datasets/`、`exp/`、`*.ckpt`、`__pycache__/` 等
（详见 Requirement 1）。Ray 解压后路径例如
`/tmp/ray/session_*/runtime_resources/working_dir_files/.../`，driver 从环境变量
读出后作为 cwd。

### MinIO 对象命名约定

```
s3://raytrain-code/{user}/{job_name}.zip                  # 默认按 user 隔离
s3://raytrain-code/_blobs/{sha256}.zip                    # 启用 dedup 时复用
```

`raytrain-code` bucket 配 7 天 lifecycle；rule 不区分前缀（_blobs 与 user/ 同 7 天）。

### MLflow tag

| key                      | value                                               |
|--------------------------|-----------------------------------------------------|
| `raytrain.code_uri`      | `s3://raytrain-code/zhangsan/zhangsan-...zip`       |
| `raytrain.code_hash`     | `sha256:a3f8...`                                    |
| `raytrain.code_size_bytes` | `91234567`                                        |
| `raytrain.image`         | `172.31.9.104:5050/training/pointcept:env-only-1.2` |
| `raytrain.cluster_mode`  | `per_job` / `shared`                                |
| `raytrain.gpu_type`      | `h20`                                               |

### Manifest 新增片段（YAML）

```yaml
code_sync:
  enabled: true
  bucket: raytrain-code
  extra_excludes:
    - "outputs/"
    - "wandb/"
  dedup: false   # nice-to-have
```

### User_Config 新增字段

```yaml
code_bucket: raytrain-code
submission_server: https://raytrain.internal.example.com   # Phase 2
token: <jwt or oidc id token>                              # Phase 2
shared_clusters:
  h20: http://ray-shared-h20-head.ray-shared.svc:8265
  a100: http://ray-shared-a100-head.ray-shared.svc:8265
default_cluster_mode: per_job   # 灰度后改成 shared
```

---

## Error Handling

按客户端 / Driver / Server 三段分别给出错误处理表。

### 客户端（CLI）

| 故障                                   | 行为                                           |
|---------------------------------------|------------------------------------------------|
| zip 大小 > 200MiB                     | 终止；列出 top-10 大文件 + 修改建议            |
| `.raytrainignore` 解析失败              | 终止；指出哪一行不合法                          |
| MinIO 5xx / 网络错误                  | 自动重试 3 次（2/4/8s 退避），仍失败则终止     |
| MinIO 4xx（403 / NoSuchBucket）       | 直接终止；提示用户检查凭据 / 调用运维建桶      |
| zip 已上传但 K8s apply 失败          | 终止；保留已上传的 zip（lifecycle 会清）        |
| Ray Job Submission API 5xx (Phase 2) | 重试 3 次；仍失败则终止                        |
| Token 过期 (Phase 2)                  | 客户端友好提示 + `raytrain configure --token`  |

### Driver

| 故障                                     | 行为                                                |
|-----------------------------------------|-----------------------------------------------------|
| working_dir 解压超时                     | Ray 自身报错；driver 从 KubeRay event 转写到日志    |
| working_dir 解压成功但 entrypoint 不存在 | driver 立即报错并退出，提示 user 检查 launcher.entrypoint |
| code_hash 与 manifest 不一致             | warn（不阻断），用户自己决定要不要继续              |

### Server (Phase 2)

| 故障                            | 行为                              |
|--------------------------------|-----------------------------------|
| Token 不合法                    | 401 + audit 一条 deny            |
| Token 合法但 tenant_isolation 拒绝 | 403 + audit 一条 deny            |
| Ray cluster 不可达              | 502 + 提示运维查 cluster 状态     |
| code_uri 无法访问               | 400 + 明确提示重新 push code_zip |

---

## Testing Strategy

### 单元测试（pytest）

**Phase 1**：

1. `tests/test_code_sync.py`
   - `build_code_zip` 排除规则正确（构造 fake repo，断言 zip 内容）。
   - 大小超限抛 `CodeSyncTooLargeError`。
   - SHA256 稳定（同输入两次哈希一致）。
   - 默认 ignore + `.raytrainignore` 合并语义。

2. `tests/test_render.py` 扩展
   - 启用 `code_sync` 时，渲染出的 RayJob YAML 中 `runtimeEnvYAML.working_dir`
     等于预期 s3 URI；`RAYTRAIN_CODE_HASH` 正确。
   - 关闭 `code_sync` 时，模板中无 `working_dir`、无 `RAYTRAIN_CODE_*`。

3. `tests/test_driver_workdir.py`
   - 模拟 `RAY_RUNTIME_ENV_WORKING_DIR=/tmp/foo` 时 `_resolve_workdir` 返回该值。
   - 缺失环境变量时退化到 manifest workdir。

**Phase 2**：

4. `tests/test_server_auth.py`
   - 合法 JWT 通过；过期 JWT 被拒；签名错误被拒。
   - OIDC ID Token via mock JWKS。

5. `tests/test_server_submit.py`
   - POST `/v1/jobs` 调 `JobSubmissionClient.submit_job` 一次（mock）。
   - submission_id 命名规则。

6. `tests/test_cli_cluster_mode.py`
   - `cluster_mode=shared` 时不调 K8s API，只调 server REST。
   - `cluster_mode=per_job` 与现有行为完全一致。

### 集成测试（手工）

- Phase 1 上线前：在 dev namespace 用 working_dir 模式跑通 pointcept smoke run。
- Phase 2 上线前：在 dev namespace 部署一份 ray-shared-h20，先打通 `--cluster-mode shared`
  在白名单用户上跑 1 周。

### 回归保护

- `make test` CI 跑全部单测。
- `make image` 仍可正常打镜像（image 层面不变）。
- 提交时 `--no-code-sync` 路径必须始终能用，作为永久回退手段。

---

## Migration Plan

### Phase 1 落地步骤（建议 1–2 周）

1. 部署 `raytrain-code` bucket + 7 天 lifecycle。
2. 发 `raytrain` 新版本（`code_sync.enabled: true` 默认开）；release notes 显
   著说明默认行为变化。
3. 在 1 个 namespace 灰度 3 天，监控：
   - zip 平均大小、p95 大小
   - working_dir 解压失败率
   - MLflow code_hash 覆盖率
4. 全量推开。保留 `--no-code-sync` 作为永久回退开关。

### Phase 2 落地步骤（建议 4–6 周）

1. 部署 `raytrain-shared` 命名空间 + 1 份 `ray-shared-h20`。
2. 部署 Submission_Server（K8s Deployment + Service）；先做 `/healthz` 自测。
3. 给 5 名白名单用户颁发 raytrain JWT，让其在 `--cluster-mode shared` 下试跑。
4. 把同一组用户的 default_cluster_mode 切到 shared，跑 1–2 周稳定后扩大白名单。
5. 全量切换：
   a. `set-default-cluster-mode.sh shared`（namespace ConfigMap）。
   b. 公告"若需回退，在 CLI 加 `--cluster-mode per_job`"。
6. 至少跑满 1 个月稳定后，开始废弃 per-user kubeconfig：
   a. 不再下发新 kubeconfig，所有新用户只发 JWT。
   b. `raytrain configure` 引导用户填 `submission_server` + `token`，不再询问 K8s。
7. 6 个月后，`--cluster-mode per_job` 标记 deprecated，再 6 个月后删除。

---

## Open Questions

下列问题在设计阶段不影响主体方向，但实施前应确认：

1. Ray 2.54 的 `runtime_env.working_dir` 对 s3 zip 的支持是否需要安装
   `smart_open` / 额外 plugin？（实测：需要 worker 镜像里装 boto3 / aiobotocore；
   现有镜像已装。）
2. 公司 SSO 是否颁发 JWT 形式的 ID Token？如果只有 SAML，需要中间桥接服务。
3. 长寿 RayCluster 升级 Ray 版本时，旧 submission 是否平滑迁移？
   建议：升级前停止接受新 submission，等所有 in-flight 任务跑完再 rolling-update。
4. `raytrain-code` bucket 是否需要 per-user 配额？
   保守值：每用户 5GiB（约 50 次 100MiB 提交），超过则报警；7 天 lifecycle
   会保证总体上限。
