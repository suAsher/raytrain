# Code-as-Submission（M0：改完代码不用 build 镜像）

raytrain 自 v0.2 起默认启用「代码即提交」：执行 `raytrain submit` 时，平台会
自动把当前工作目录打包上传到 MinIO，由 Ray 的 `runtime_env.working_dir` 在每个
worker 上拉取并解压，然后训练子进程在解压后的目录里运行。

**结果**：你改一行 `tools/train.py`，直接 `raytrain submit`，集群里跑的就是新代码。
不需要 docker build，不需要 docker push。

---

## 它是怎么工作的

```
raytrain submit
   │
   ├─[1/5] 打包当前目录       (zip + sha256，排除 .git/data/exp 等)
   ├─[2/5] 上传到 MinIO       (raytrain-code/<user>/<job>.zip)
   ├─[3/5] 创建 MLflow run    (附 code_uri / code_hash 标签)
   ├─[4/5] 渲染 RayJob YAML   (runtimeEnvYAML.working_dir = 上面的 s3 URI)
   └─[5/5] 提交到 K8s
                              │
                              ▼
                    Ray 在每个 worker 上：
                    - 从 MinIO 拉 zip
                    - 解压到临时目录
                    - 暴露 RAY_RUNTIME_ENV_WORKING_DIR
                    - driver / 训练子进程 chdir 进去
```

---

## 默认行为

第一次接入，你**什么都不用改**。raytrain 默认会按下面的规则打包当前目录：

### 默认排除规则（不进 zip）

```
.git/  .hg/  .svn/
__pycache__/  *.pyc  *.pyo
.pytest_cache/  .mypy_cache/  .ruff_cache/  .tox/
*.egg-info/  build/  dist/
.venv/  venv/  env/
.idea/  .vscode/
node_modules/
data/  datasets/  exp/  outputs/  logs/  wandb/
*.ckpt  *.pth  *.pt  *.tar  *.safetensors
.DS_Store  Thumbs.db  *.swp
.raytrain-cache/
```

外加：仓库根的 `.gitignore` 里所有规则也会被自动应用。

### 上限

单个 zip 不超过 **200 MiB**。超过会报错并列出 top-10 大文件，让你自己决定排除什么。

---

## 自定义排除：`.raytrainignore`

如果你的项目还有别的不想进 zip 的目录或文件，在仓库根放一个 `.raytrainignore`，
和 `.gitignore` 同语法：

```
# .raytrainignore
outputs/
runs/
*.parquet
events.out.tfevents.*
```

完整模板见 `.raytrainignore.example`。

也可以在 `.raytrain.yaml` 里配 `code_sync.extra_excludes`：

```yaml
code_sync:
  enabled: true
  extra_excludes:
    - "outputs/"
    - "*.parquet"
```

---

## 关闭 / 临时回退

`raytrain submit --no-code-sync` 一次性回到老路径（代码必须打在镜像里）。

或者在 `.raytrain.yaml` 里永久关掉：

```yaml
code_sync: false
# 或
code_sync:
  enabled: false
```

---

## 关键 CLI flag

| Flag | 默认 | 说明 |
|---|---|---|
| `--no-code-sync` | off | 跳过打包/上传，回到镜像内代码模式 |
| `--workdir-zip <path>` | off | 用一份预构建的 zip 替代当前目录 |
| `--code-bucket <name>` | `raytrain-code` | 改用其它 MinIO bucket（个人 / 团队 bucket） |

---

## `.raytrain.yaml` 里的 `code_sync` 字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `bucket` | `raytrain-code` | MinIO bucket |
| `extra_excludes` | `[]` | gitignore 风格 pattern 列表 |
| `max_size_mib` | `200` | 单 zip 大小上限 |
| `dedup` | `false` | 启用时按 sha256 跨 user 复用 zip（实验性） |

---

## MinIO bucket 怎么准备（运维一次性）

```bash
MINIO_ENDPOINT=http://172.31.16.3:30950 \
MINIO_ACCESS_KEY=xxx \
MINIO_SECRET_KEY=xxx \
    deploy/setup-code-bucket.sh
```

脚本做的事（幂等）：
1. 创建 `raytrain-code` bucket（已存在则跳过）
2. 写入 7 天 lifecycle policy（"对象创建 7 天后自动删除"）

**注意**：超过 7 天的训练 run 没法用 `raytrain reproduce`（待实现）恢复历史代码。
若业务需要更长，把 `LIFECYCLE_DAYS=30` 之类的参数传进去即可。

---

## 给 driver / 训练子进程的环境变量

启用 code-sync 时，平台向训练子进程注入这些环境变量（便于审计 / 复现）：

| 变量 | 含义 |
|---|---|
| `RAYTRAIN_CODE_URI` | 代码 zip 在 MinIO 上的 s3 URI |
| `RAYTRAIN_CODE_HASH` | 代码 zip 的 SHA256（hex） |
| `RAYTRAIN_CODE_SIZE_BYTES` | zip 大小（字节） |
| `RAYTRAIN_RESOLVED_WORKDIR` | 训练子进程实际 cwd 的绝对路径 |
| `RAY_RUNTIME_ENV_WORKING_DIR` | Ray 自身设置；driver 用来定位代码 |

训练代码可以读 `RAYTRAIN_CODE_HASH` 写到 MLflow tag 里，方便事后定位"这次 run
跑的是哪份代码"。

---

## 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `[1/5] packaging code...` 后报 `code zip is N MiB, exceeds limit 200 MiB` | 工作目录里有大文件被包进来 | 看错误里的 top-10 列表，加到 `.raytrainignore` |
| `[2/5] uploading code...` 报 `AccessDenied` | MinIO 凭据没权限写 `raytrain-code` bucket | 联系运维确认 user policy 包含 `raytrain-code` |
| `[2/5]` 报 `NoSuchBucket` | bucket 还没创建 | 运维执行 `deploy/setup-code-bucket.sh` |
| 任务 head pod 报 `failed to download working_dir` | MinIO 在集群内不可达 / 凭据错 | 在 head pod 内 `curl` MinIO endpoint 验证 |
| 任务起来但提示 `no workdir resolved` | 既没启用 code-sync 也没设 `workdir` | 在 manifest 里设 `workdir` 或开 `code_sync` |
| 训练子进程报"找不到 configs/xxx.py" | code zip 里没有这个文件（被排除规则屏蔽了？） | 用 `--workdir-zip` 把 zip 拉本地 `unzip -l` 看看 |

---

## 与现有功能的关系

- **`datasets:` mount 模式**：保持兼容。`code_sync` 只管代码，不管数据。
- **`data_source:` Lance 流式模式**：保持兼容。Ray Data 通过环境变量读 URI，
  不需要打到 zip 里。
- **MLflow 上传 artifact**：保持不变。code-sync 只影响"代码进集群"，不影响
  "产物出集群"。
- **`raytrain logs` / `exec` / `stop`**：完全不变。
