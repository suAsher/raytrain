# raytrain 运维手册

面向对象：平台运维。涵盖命名空间、节点标签、RBAC、镜像体系、数据迁移、故障排查。

## 1. 命名空间和节点

### 创建命名空间

```bash
kubectl create namespace ray-cluster-3 || true
```

### 节点标签

```bash
# 一次即可
kubectl label node cactus gpu=a100 --overwrite
kubectl label node H20    gpu=h20  --overwrite
kubectl label node H21    gpu=h20  --overwrite
# H20-2 加入 rke2 之后：
# kubectl label node H20-2 gpu=h20 --overwrite

kubectl get nodes -L gpu
```

或直接跑 `bash raytrain/deploy/node-labels.sh`（可重复执行）。

### GPU worker 节点的 hostPath

模板里这两个挂载必须在节点上存在（本身是目录即可，不需要 PV/PVC）：

| Pod 内路径         | 宿主机路径            | 用途                        |
| ------------------ | --------------------- | --------------------------- |
| `/mnt/ray-cache`   | `/data4/ray-cache`    | 数据集缓存 + 实验输出目录   |
| `/mnt/ray-spill`   | `/data3/ray-spill`    | Ray object-store spill      |

## 2. RBAC（每用户一次）

模板在 `raytrain/deploy/rbac-raytrain-user.yaml`。编辑 `subjects[0].name` 为用户身份，再 apply：

```bash
kubectl apply -f raytrain/deploy/rbac-raytrain-user.yaml
```

Ray head/worker Pod 默认用 `default` ServiceAccount。若启用了 NetworkPolicy / PSA，要确保：
- 该 SA 能访问 MinIO 和 MLflow
- 模板里的 hostPath（`/data3`、`/data4`）允许挂载

## 3. 配额（可选）

```yaml
# raytrain/deploy/resource-quota.yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: raytrain-quota
  namespace: ray-cluster-3
spec:
  hard:
    requests.nvidia.com/gpu: "16"
```

`kubectl apply -f raytrain/deploy/resource-quota.yaml`。超额的任务自动 Pending 排队。

## 4. 镜像体系

### 结构

```
pytorch/pytorch:2.5.0-cuda12.4-cudnn9-devel          ← 上游
        │
        ▼
training/base-ray-pytorch:ray2.54.1-torch2.5.0-cu124 ← 装 ray/mlflow/boto3/minio/jinja2/click 等
        │                                              （不含 raytrain 源码，只装 raytrain 的依赖）
        ▼
training/pointcept:ray2.54.1-torch2.5.0-cu124        ← 加 Pointcept 代码 + CUDA 扩展 + **最末层装 raytrain**
training/<其他项目>:...                               ← 未来的项目，同样 FROM base + 最末层装 raytrain
```

**关键**：raytrain 装在**每个项目镜像的最末一层**（不装 base）。好处是改 raytrain
只让该项目镜像最末层失效，重建 10 秒。坏处是每个新项目 Dockerfile 最后都要加
一段 COPY raytrain + pip install，不过是 3 行事。

### 构建 + 推送

一键脚本：`bash dockerfile/build.sh`（在 `pointcept-main` 仓库根目录执行）。内容等价于：

```bash
# 1. 构建 base（含 raytrain 源码，含 raytrain 的第三方依赖）
DOCKER_BUILDKIT=1 docker build \
    -f dockerfile/Dockerfile.base-ray-pytorch \
    --build-arg RAY_VERSION=2.54.1 \
    --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    -t 172.31.9.104:5050/training/base-ray-pytorch:ray2.54.1-torch2.5.0-cu124 .
docker push 172.31.9.104:5050/training/base-ray-pytorch:ray2.54.1-torch2.5.0-cu124

# 2. 构建 pointcept
DOCKER_BUILDKIT=1 docker build \
    -f dockerfile/Dockerfile.pointcept \
    -t 172.31.9.104:5050/training/pointcept:ray2.54.1-torch2.5.0-cu124 .
docker push 172.31.9.104:5050/training/pointcept:ray2.54.1-torch2.5.0-cu124
```

脚本支持 `bash dockerfile/build.sh base`（只 base）或 `bash dockerfile/build.sh pointcept`（只项目）。
设 `PUSH=0` 可以只构建不推送。

### base 镜像加了什么

`Dockerfile.base-ray-pytorch` ：

```dockerfile
# raytrain 依赖（python 包，非 raytrain 源码本身）
# raytrain 源码装在每个项目镜像的最末层，base 只装第三方依赖保持稳定
RUN python -m pip install \
      "minio>=7.2" "jinja2>=3.1" "click>=8.1"
```

### pointcept 镜像最末层装 raytrain

`Dockerfile.pointcept` 的最后一组 layer：

```dockerfile
# 最末层：COPY raytrain + pip install
COPY raytrain /tmp/raytrain
RUN pip install --force-reinstall --no-deps /tmp/raytrain && rm -rf /tmp/raytrain
RUN python -c "from raytrain.entrypoint import driver; print('OK')"
```

### 构建 context 的坑

**base 镜像**：构建命令 `-f dockerfile/Dockerfile.base-ray-pytorch .`，context 就是
`pointcept-main` 根目录，但 base Dockerfile 里**不再 COPY raytrain**，context
里有没有 raytrain/ 都无所谓。

**pointcept 镜像**：构建命令 `-f dockerfile/Dockerfile.pointcept .`，context
也是 `pointcept-main` 根目录，Dockerfile 在最末层 `COPY raytrain /tmp/raytrain`
把 raytrain 源码拷进镜像。所以 `.dockerignore` 里**不能**排除 `raytrain/`，
否则会报 "raytrain: not found"。

### 加速构建

flash-attn 源码编译要 10+ 分钟，可换预编译 wheel：

```dockerfile
RUN pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.7.4.post1/\
flash_attn-2.7.4.post1+cu12torch2.5cxx11abiFALSE-cp311-cp311-linux_x86_64.whl || true
```

（对应 python 3.11 + torch 2.5 + cu12 + ABI=False）

### 改 raytrain 代码后要不要重建镜像？

**关键优化**：raytrain 装在**每个项目镜像的最末层**（不装 base），
这样改 raytrain 只让最后一层缓存失效，重建 **10 秒以内**，不会重编译任何 CUDA 扩展。

| 改动范围 | 要做什么 | 耗时 |
|---|---|---|
| `raytrain/cli/`、`raytrain/templates/`、`raytrain/manifest.py`、`raytrain/rayjob.py` 等 **只在 Kasm 跑的代码** | Kasm 上 `cd ~/raytrain && git pull && pip install -e .` | 秒级 |
| `raytrain/entrypoint/driver.py`、`launchers.py`、`dataset_sync.py` 等 **运行在 Pod 里的代码** | `bash dockerfile/build.sh pointcept` 重建项目镜像最末层 | ~10 秒 |
| 新增/修改 `launcher.type`（例如 `ray_train`） | CLI 侧和 Pod driver 侧都要更新：Kasm 重新 `pip install -e .`，并重建使用该 launcher 的项目镜像最末层 | 秒级到 ~10 秒 |
| `Dockerfile.pointcept` 改了 `pip install` 层或前面任何层 | `bash dockerfile/build.sh pointcept` | 视改动位置，最长 ~10 分钟 |
| `Dockerfile.base-ray-pytorch` 改了 | `bash dockerfile/build.sh`（base + pointcept 全量） | ~20 分钟 |

**镜像分层原则**：Dockerfile.pointcept 的 RUN 命令按"变化频率从低到高"排列：
固定 pip 依赖 → CUDA 扩展编译 → 项目 python 源码 → **raytrain 源码（最末层）**。
改 raytrain 只触发最后一个 COPY + pip install，Docker 自动命中前面所有层的缓存。

**特殊情况**：改 `Dockerfile.base-ray-pytorch` 会生成新的 base digest，
导致项目镜像 `FROM base` 层失效、整个 pointcept 镜像从头重建（慢）。
所以**不要轻易改 base**，base 只负责 CUDA/torch/ray/mlflow 这些基础依赖。

## 5. 数据迁移

H20-2 上 `/storage/data-acc` 的 10TB 数据一次性迁 MinIO：

```bash
# 在 H20-2 上执行
MINIO_ACCESS_KEY=... MINIO_SECRET_KEY=... \
    bash raytrain/deploy/migrate-data-to-minio.sh
```

脚本内部就是 `mc mirror`，可重复执行（幂等）。命名约定：

| MinIO 路径                              | 内容                                |
| --------------------------------------- | ----------------------------------- |
| `s3://pointcept-data/<dataset>/…`       | 共享只读数据集                      |
| `s3://u-<user>-exp/<run-id>/…`          | 各用户实验输出                      |
| `s3://u-<user>-scratch/…`               | 用户临时空间                        |

## 6. 故障排查速查

### 任务一直 Pending

```bash
kubectl -n ray-cluster-3 describe rayjob <job-name>
kubectl -n ray-cluster-3 get pods -l ray.io/cluster
kubectl -n ray-cluster-3 describe pod <pending-pod> | tail -30
kubectl -n ray-cluster-3 get events --sort-by='.lastTimestamp' | tail -20
```

常见原因：GPU 不够、节点没标签、hostPath 目录缺失、镜像拉不动（内网 registry 证书/权限问题）。

### driver 报 "placement group not ready"

满足 nodeSelector 的节点 GPU 不够。用 `kubectl describe nodes -l gpu=h20 | grep gpu` 查看可分配数。

注意：`launcher.type: ray_train` 不走 driver placement group，也不会创建 GPU
`NodeLauncher` actor。如果 `ray_train` 任务报 GPU pending，优先看 Ray Train /
TorchTrainer worker 资源请求，而不是这个 placement group 分支。

### ray_train 任务提交成功但 TorchTrainer worker 一直 pending

常见原因：

1. `.raytrain.yaml` 里 `--cpus-per-worker` 写死过大。
2. `resources.cpus_per_node / resources.gpus_per_node` 小于训练入口内部 `resources_per_worker["CPU"]`。
3. 训练入口没有使用 raytrain 展开的 `{cpus_per_worker}`，仍然固定申请 8 CPU。

排查：

```bash
raytrain logs <job> -f
raytrain exec <job>       # 进 head
ray status
```

建议 `.raytrain.yaml`：

```yaml
launcher:
  type: ray_train
  args:
    - --num-workers
    - "{world_size}"
    - --cpus-per-worker
    - "{cpus_per_worker}"
```

训练入口里用 `--cpus-per-worker` 设置 `TorchTrainer(... resources_per_worker={"CPU": value, "GPU": 1})`。

### Submitter pod 报 `ModuleNotFoundError: No module named 'raytrain'`

Ray runtime_env 隔离了 site-packages。模板里 entrypoint 已经用
`bash -c 'export PYTHONPATH=/opt/conda/lib/python3.11/site-packages; exec python -m raytrain.entrypoint.driver ...'`
绕开。如果仍然报，说明镜像里 `/opt/conda/lib/python3.11/site-packages/raytrain/`
不存在（base 层构建失败或被跳过），检查构建日志。

### NCCL 第 0 步卡住

```bash
raytrain exec <job> --worker 0
env | grep NCCL
ip addr
```

默认 `NCCL_IB_DISABLE=1`、`NCCL_SOCKET_IFNAME=^lo,docker0`。如果集群主网卡不是这个值，
优先在项目 `.raytrain.yaml` 里覆盖：

```yaml
launcher:
  env:
    NCCL_SOCKET_IFNAME: eth0
    GLOO_SOCKET_IFNAME: eth0
```

模板现在只在 manifest 没写时补 NCCL 默认值，`launcher.env` 会优先。

### Submitter pod 报 `directory None must be an existing directory`

模板里 `working_dir` 被误渲染成字符串 "None"。当前版本已去掉这行；如果你拿到的是旧版本，
重新 rsync `raytrain/` 并 `pip install -e .`。

### 查看某节点上的数据缓存

```bash
ssh H20 ls -la /data4/ray-cache/datasets/
ssh H20 cat /data4/ray-cache/datasets/.done/scannet.done
```

### 清理卡住的残留

```bash
kubectl -n ray-cluster-3 delete rayjob <name>
kubectl -n ray-cluster-3 delete configmap raytrain-payload-<name>
kubectl -n ray-cluster-3 delete secret    raytrain-creds-<name>
```

或一把清理命名空间下所有 raytrain 相关对象：

```bash
kubectl -n ray-cluster-3 delete rayjob --all
kubectl -n ray-cluster-3 delete configmap -l app!=kube-dns --ignore-not-found
kubectl -n ray-cluster-3 delete secret    --field-selector type=Opaque --ignore-not-found
```

## 7. 升级 raytrain 框架的流程

1. 在 `raytrain/` 源码目录里改代码
2. 本地跑 `cd raytrain && python tests/test_render.py` 确保渲染通过
3. push 到内网 GitLab（或手动 rsync）
4. 判断改的是 CLI 侧还是 driver 侧：
   - CLI 侧：通知用户 `cd ~/raytrain && git pull && pip install -e .`
   - Driver 侧：`bash dockerfile/build.sh` 重建镜像
   - launcher 新能力（如 `ray_train`）：两侧都要更新，因为 CLI 要能解析 manifest，Pod 里的 driver 要能执行新 launcher
5. 冒烟一个小任务验收

### ray_train launcher 的运维语义

`ray_train` 和 `native_ddp` 的资源路径不同：

| 模式 | driver 行为 | GPU 谁申请 |
|---|---|---|
| `native_ddp` | 创建 placement group 和每节点 `NodeLauncher` actor | `NodeLauncher` actor 预占整节点 GPU |
| `ray_train` | 不创建 GPU `NodeLauncher`，head 中只启动一次 entrypoint | 训练入口内部的 Ray Train / `TorchTrainer` worker 申请 GPU |

因此：

- `native_ddp` 失败常看 placement group、节点 IP、`[node0]` 日志。
- `ray_train` 失败常看 head 日志、Ray Train worker pending、`ray status`。
- `launcher.env`、`data_source`、MinIO/MLflow 凭据必须注入到 Ray pod 环境，因为 Ray Train worker 是新的 Ray actor，不是 driver 直接 fork 出来的子进程。

## 8. 删除一个用户

```bash
# 撤回 RBAC
kubectl -n ray-cluster-3 delete rolebinding raytrain-user-<user>

# 取消正在跑的任务
kubectl -n ray-cluster-3 get rayjob -l raytrain.owner=<user> -o name | \
    xargs -r kubectl -n ray-cluster-3 delete

# MinIO 的 u-<user>-exp / u-<user>-scratch bucket 按需归档或删除
```

## 9. Code Bucket 运维

Phase 1（code-as-submission）后，`raytrain submit` 默认会把当前工作目录打包成 zip
上传 MinIO，集群侧 Ray 通过 `runtime_env.working_dir` 自动拉取并解压。这个 zip 存放
在一个专用 bucket 里，本节讲它的运维。

### bucket 名与作用

| 项 | 值 |
| --- | --- |
| 默认 bucket | `raytrain-code` |
| 覆盖方式 | CLI `--code-bucket <name>`，或 `.raytrain.yaml` 的 `code_sync.bucket` |
| key 约定 | `<user>/<job_name>.zip`（启用 dedup 时另有 `_blobs/<sha256>.zip`） |
| 作用 | 承载 M0 code-as-submission 的 `working_dir` 来源，让用户改代码不必重 build 镜像 |

zip 内只放代码，不含 `.git/`、`data/`、`datasets/`、`exp/`、checkpoint 等
（排除规则见 `docs/user-guide.md`）。`raytrain reproduce <mlflow_run_id>` 也是从这个
bucket 回拉历史代码的。

### 创建 / 初始化

用 `deploy/setup-code-bucket.sh` 建桶并写 lifecycle，脚本幂等（bucket 已存在则复用，
lifecycle 重复 import 不会报错）：

```bash
MINIO_ENDPOINT=http://172.31.16.3:30950 \
MINIO_ACCESS_KEY=xxx \
MINIO_SECRET_KEY=xxx \
    bash raytrain/deploy/setup-code-bucket.sh

# 覆盖 bucket 名（要和用户侧 code_sync.bucket / --code-bucket 一致）：
bash raytrain/deploy/setup-code-bucket.sh my-team-code
```

脚本会创建 bucket、写入"对象创建 N 天后自动删除"的 lifecycle，并打印一行 sanity
信息。运行前需要本机装好 `mc`，且 endpoint/access_key/secret_key 可用。

### lifecycle（7 天过期）

bucket 默认配 7 天硬过期：任何 zip 对象创建满 7 天后由 MinIO 自动删除。这直接决定了
`raytrain reproduce` 的回溯窗口——

- 7 天内的 run：`code_uri` 指向的对象还在，能精确还原代码。
- 超过 7 天的 run：对象已被 lifecycle 删除，`raytrain reproduce` 会友好报错，
  只能改用 git commit 关联回溯。

如需调整保留期，用环境变量 `LIFECYCLE_DAYS` 重新跑脚本（会覆盖原 rule）：

```bash
# 改成保留 30 天
LIFECYCLE_DAYS=30 \
MINIO_ENDPOINT=http://172.31.16.3:30950 \
MINIO_ACCESS_KEY=xxx MINIO_SECRET_KEY=xxx \
    bash raytrain/deploy/setup-code-bucket.sh
```

延长保留期会按比例放大 bucket 占用，调整前先估算容量（见下）。

### 配额建议

- 单个 zip 上限 200 MiB，由用户侧 `.raytrain.yaml` 的 `code_sync.max_size_mib`
  控制（超限会在客户端直接报错并列出 top-10 大文件，不会上传）。
- bucket 容量粗估：`并发活跃任务数 × 平均 zip 大小 × 7 天内的提交次数`。
  例如 20 人、每人每天提交 10 次、平均 50 MiB，7 天滚动窗口约
  `20 × 10 × 7 × 50 MiB ≈ 68 GiB`。
- 保守上限建议按每用户 5 GiB（约 50 次 100 MiB 提交）规划；7 天 lifecycle 本身已
  限制总量，但仍建议给 bucket 配额或监控，避免异常大 zip / 高频提交打满磁盘。
- 启用 `code_sync.dedup: true` 时相同内容只存一份 `_blobs/<sha256>.zip`，能显著
  降低重复提交的占用，但 lifecycle 仍是 7 天。

监控可用 `mc` 直接看 bucket 用量：

```bash
mc du raytrain-setup/raytrain-code
mc ls --summarize raytrain-setup/raytrain-code/
```

### 紧急清理操作

> ⚠️ 风险提示：清理正在被某个任务拉取的 zip 会导致该任务 worker 启动失败
> （`working_dir` 拉不到）。清理前先确认目标 zip 对应的任务已结束。正常情况下交给
> lifecycle 自动过期即可，手动删除只用于磁盘告急或误传大文件等紧急场景。

下面的 `<alias>` 指 `mc alias set` 配过的别名（脚本里用的是 `raytrain-setup`）。

```bash
# 列出某用户的所有 code zip
mc ls raytrain-setup/raytrain-code/zhangsan/

# 删除某用户某个任务的 zip
mc rm raytrain-setup/raytrain-code/zhangsan/zhangsan-pointcept-exp1.zip

# 递归删除某用户的全部 zip（删用户时配合 RBAC 回收）
mc rm --recursive --force raytrain-setup/raytrain-code/zhangsan/

# 按时间清理：删除 7 天前的对象（lifecycle 失效时的兜底手段）
mc rm --recursive --force --older-than 7d raytrain-setup/raytrain-code/
```

lifecycle 是 MinIO 后台异步扫描执行的，无法"手动立即触发"；如果急需释放空间，用上面
的 `mc rm --older-than` 直接删，效果等价于提前执行过期。删除后可再次确认 lifecycle
规则仍在：

```bash
mc ilm export raytrain-setup/raytrain-code
```

### 验证命令

```bash
# bucket 存在、看对象
mc ls raytrain-setup/raytrain-code/

# 查看 lifecycle（应包含 Days: 7 的过期规则）
mc ilm export raytrain-setup/raytrain-code
```
