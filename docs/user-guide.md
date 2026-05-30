# 建模同学使用手册

面向对象：在 Kasm 桌面上提交训练任务的同学。前提你已经走过一遍 `quickstart.md`
（装了 CLI、配了凭据、拿到 kubectl 权限）。

项目级完整操作手册：

- `pointcept-main` 和 `sslod26-master` 的镜像构建、dry-run、单卡 smoke、多卡/多机、日志和排错步骤见
  [Pointcept 与 sslod26 使用 raytrain 提交训练完整手册](pointcept-sslod26-raytrain-runbook.md)。

## 核心心智模型

记住四件事就够了：

1. **`raytrain/` 框架源码**：只在 Kasm 用来提交任务，和训练没关系。放 `~/raytrain/`。
2. **训练项目仓库**：你要训的代码（如 `pointcept-main/`），根目录必有 `.raytrain.yaml`。
3. **`~/.raytrain/config.yaml`**：你的 MinIO/MLflow 凭据，`raytrain configure` 生成。
4. **镜像**：训练镜像（如 `pointcept:ray2.54.1-torch2.5.0-cu124`）由运维构建好并推到内网 registry，你不用管。

日常你只操作**项目仓库**和**CLI**。

## 1. 怎么提交任务

**必须在项目根目录执行**（`.raytrain.yaml` 要能被 CLI 读到）：

```bash
cd ~/pointcept-main

# 单机单卡（冒烟测试）
raytrain submit \
    --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 1 --nodes 1 --gpu-type h20 --name smoke

# 单机 8 卡
raytrain submit \
    --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 8 --nodes 1 --gpu-type h20 --name ptv3-single

# 多机 2×8
raytrain submit \
    --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 8 --nodes 2 --gpu-type h20 --name ptv3-2node
```

**submit 常用参数**：

| 参数 | 作用 |
|---|---|
| `--config <path>` | 训练 config 文件（相对仓库根路径） |
| `--gpus <N>` | 每节点 GPU 数 |
| `--nodes <N>` | 节点数，1 = 单机 |
| `--gpu-type h20 \| a100` | 调度到哪个 GPU 池 |
| `--name <str>` | 实验名，默认取 config 文件名 |
| `--dry-run` | 只打印生成的 YAML，不真实提交 |
| `--image <tag>` | 覆盖 `.raytrain.yaml` 里的镜像 |
| `--config-override key=value` | 追加给训练代码的 `--options` 覆盖 |
| `--help` | 看所有参数 |

### `.raytrain.yaml` 要每次改吗？

**不需要**。它是**每个项目一次性**配置文件（类似 `Dockerfile`），只有在以下情况才改：
- 换镜像 tag
- 新增一个数据集声明
- 改默认 GPU / 内存 / shm 规格
- 训练入口类型变化（例如从原生 DDP 切到 Ray Train/TorchTrainer）

日常改 config、改 GPU 数都走 CLI 参数，不动 yaml。

### launcher 类型是什么？

`.raytrain.yaml` 里的 `launcher.type` 决定 raytrain 怎么启动训练。当前支持：

| launcher | 适用训练代码 | raytrain 行为 |
|---|---|---|
| `native_ddp` | 项目入口自己支持 `--num-gpus / --num-machines / --machine-rank / --dist-url`，例如 Pointcept `tools/train.py` | 每个 GPU 节点创建一个 `NodeLauncher` actor，actor 里启动项目训练脚本，项目自己 `mp.spawn` |
| `torchrun` | 标准 PyTorch DDP，依赖 `torchrun` 注入 `RANK/LOCAL_RANK/WORLD_SIZE` | 每个节点拼出 `torchrun --nnodes ...` 命令 |
| `accelerate` | HuggingFace Accelerate 项目 | 每个节点拼出 `accelerate launch ...` 命令 |
| `custom` | 特殊命令，但仍是“每节点一个 subprocess”的资源模型 | entrypoint 原样作为命令前缀 |
| `ray_train` | 训练入口内部使用 Ray Train / `TorchTrainer`，例如 `tools/train_ray.py` | 不创建 GPU `NodeLauncher`；只在 head 启动一次训练 driver，GPU worker 由 `TorchTrainer` 自己申请 |

`custom` 不能替代 `ray_train`。如果训练入口内部还会创建 Ray Train worker，
就必须用 `ray_train`，否则 GPU 会先被父 `NodeLauncher` actor 占住，内部
`TorchTrainer` 再申请 GPU 时可能 pending。

### `environment.yml` 是做什么的？

是 Pointcept **原作者**给想在本地用 conda 搭环境的人准备的，和 KubeRay 流程**完全无关**。迁到 KubeRay 以后用不到，所有依赖都已经在 Dockerfile 里固化到镜像里了。留着只是保留原仓库结构。

## 2. 查任务状态和日志

```bash
raytrain list                     # 列自己的任务
raytrain list --all-users         # 列集群里所有人的任务
raytrain logs <job_name> -f       # 跟 driver 日志（多节点汇流，带 [node0] 前缀）
raytrain logs <job_name> --worker 1 -f   # 某个 worker 的 ray 日志
raytrain mlflow <job_name>        # 打印 MLflow URL
raytrain mlflow <job_name> --open # 直接打开浏览器
raytrain stop <job_name>          # 取消任务
```

`raytrain list` 的 `status` 字段常见值：

- `Pending / Initializing` — 资源排队或 Pod 还没起来
- `Running` — 训练中
- `Succeeded` — 成功完成
- `Failed` — 失败，用 `raytrain logs` 看原因
- `Stopped` — 被取消

## 3. 进 Pod 调试：`raytrain exec`

类似 ssh，但实际上是 `kubectl exec` 封装：

```bash
raytrain exec <job_name>               # 列出所有 Running Pod，交互选择进哪个
raytrain exec <job_name> --worker 0    # 直接进第 0 个 worker
raytrain exec <job_name> --shell sh    # 用 sh 替代 bash
```

不带 `--worker` 时会列出所有 Pod 让你选：

```
Available pods:
  [0] ...-head-xxx    (head, node=h20)
  [1] ...-worker-xxx  (worker, node=h20)

Enter pod number [0]: 1
entering ...-worker-xxx (bash)...
```

进去之后常用操作：

```bash
# 看 GPU 占用
nvidia-smi

# 看 Ray 集群状态
ray status

# 看挂载情况
ls /mnt/ray-cache /mnt/ray-spill

# 看训练进程
ps aux | grep train.py

# 手动重跑 entrypoint 做调试
cd /workspace/pointcept
python tools/train.py --config-file configs/... --num-gpus 1 ...
```

## 4. 数据读写

**三种场景**：

### 场景 A：数据已经在 MinIO（推荐）

在项目的 `.raytrain.yaml` 里声明：

```yaml
datasets:
  - {name: scannet, s3: s3://pointcept-data/scannet, mount: data/scannet}
```

- **首次任务**：driver 自动把数据从 MinIO 同步到 GPU 节点的
  `/data4/ray-cache/datasets/scannet/`（Pod 内 `/mnt/ray-cache/datasets/scannet/`）
- **在 Pod 内**：`<workdir>/data/scannet` 是个 symlink 指向缓存目录
- **训练代码**：按原本 `data/scannet/...` 路径读写，完全不用改
- **后续任务**：如果同一数据集已缓存（有 `.done` 标记），**跳过下载直接用**

### 场景 B：数据已经在 GPU 节点本地盘

运维把数据 rsync 到节点的 `/data4/ray-cache/datasets/<name>/`，Pod 内自动可见为
`/mnt/ray-cache/datasets/<name>/`。

这种情况 `.raytrain.yaml` **不用声明 `datasets:`**，让训练代码直接读
`/mnt/ray-cache/datasets/<name>/`（可能需要改 Pointcept 的 config 里 `data_root` 指过去），或者自己做一次 symlink。

### 场景 D：Ray Data + Lance 流式读取（大数据集推荐）

当数据集太大（>5TB）本地盘装不下，或者希望训练立即开始不等下载时，使用流式读取模式。

在 `.raytrain.yaml` 里用 `data_source:` 替代 `datasets:`：

```yaml
# 旧方式：预下载到本地（小数据集适用）
# datasets:
#   - {name: nuscenes, s3: s3://pointcept-data/nuscenes, mount: data/nuscenes}

# 新方式：Ray Data 从 MinIO 流式读 Lance（大数据集推荐）
data_source:
  type: lance                                          # lance 或 parquet
  uri: s3://lance-datasets/nuscenes-v1/train.lance     # MinIO 上的 Lance 数据集路径
  version: latest                                      # Lance 版本号或 "latest"
  # filter: "split == 'train'"                         # 可选：Lance 过滤表达式
  # columns: [coord, strength, segment]                # 可选：只读这些列（列裁剪）

# CPU Worker 数量（配合 data_source 使用）
cpu_workers: 4
```

**`data_source:` 和 `datasets:` 互斥**，二选一。提交命令完全不变：

```bash
raytrain submit \
    --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
    --gpus 8 --nodes 2 --gpu-type h20 --name nusc-lance
```

**`cpu_workers` 是什么？**

设置 `cpu_workers: 4` 会额外启动 4 个 **CPU-only Pod**（不占 GPU），加入 Ray 集群专门做数据预处理：

```
┌─────────────────────────┐    ┌──────────────────────────┐
│  GPU Worker Pod × 2     │    │  CPU Worker Pod × 4      │
│  (8 GPU each)           │    │  (无 GPU, 8 CPU each)    │
│                         │    │                          │
│  - 训练 forward/backward│◄───│  - decode 点云            │
│  - NCCL all-reduce      │    │  - augment 数据增强       │
│  - 只消费处理好的 batch  │    │  - 结果放入 Plasma 共享内存│
└─────────────────────────┘    └──────────────────────────┘
```

- `cpu_workers: 0`（默认）= 不启动 CPU Pod，数据预处理在 GPU worker 上做
- `cpu_workers: 4` = 启动 4 个 CPU Pod，GPU worker 只做训练，CPU worker 做预处理
- 建议：点云训练设 4-8，图像训练设 2-4

**训练代码怎么改？**

训练代码里用 `raytrain.data.RayLanceDataset` 替换原来的 Dataset 类：

```python
from raytrain.data import RayLanceDataset

class MyLanceDataset(RayLanceDataset):
    def __init__(self, data_root, split="train", **kwargs):
        super().__init__(
            uri=data_root,
            filter_expr=f"split == '{split}'" if split else None,
            transform_fn=MyAugmentActor,   # 数据增强（跑在 CPU worker 上）
            batch_size=6,
            prefetch_batches=4,            # 预取 4 个 batch
            do_materialize=True,           # 缓存到 Plasma 加速后续 epoch
        )
```

底层使用 Ray Data 的全部特性：流式读取、Plasma 缓存、零拷贝、ActorPool 预处理、prefetch 流水线。

### 场景 E：训练入口本身使用 Ray Train / TorchTrainer

有些项目不是把 Ray Data 包成普通 PyTorch Dataset，而是在训练入口里直接使用
`ray.train.torch.TorchTrainer`。这种项目 `.raytrain.yaml` 应该使用：

```yaml
launcher:
  type: ray_train
  entrypoint: tools/train_ray.py
  args:
    - --config
    - "{config}"
    - --num-workers
    - "{world_size}"
    - --cpus-per-worker
    - "{cpus_per_worker}"
    - --run-name
    - "{run_name}"
    - --storage-path
    - "{save_path}"
```

关键差异：

- `ray_train` 只在 RayJob head 中启动一次 `tools/train_ray.py`
- `TorchTrainer` 自己调度 GPU worker
- `{world_size}` = `--nodes × --gpus`
- `{cpus_per_worker}` = `resources.cpus_per_node / resources.gpus_per_node`
- `data_source` 和 `launcher.env` 会注入到 Ray head/worker Pod，保证 Ray Train worker 也能读到 MinIO、Lance、NCCL、cache 等环境变量

典型日志：

```text
[driver] launcher type ray_train: running entrypoint once in head
[ray-train] launching in /workspace/...: python tools/train_ray.py ...
```

### 训练产物（checkpoint / log）怎么回传？

*   **默认本地路径**：默认写到 `/mnt/ray-cache/exp/<user>/<run-id>/`（节点本地 NVMe SSD，IO 极快）。训练结束后由 driver 自动上传到 MLflow 产物。
*   **多机训练配置**：因为多机训练时不同机器的本地 `/mnt/ray-cache` 互不相通，如果有多机通信读取 Checkpoint 的需求，或者想直接持久化，建议**在 `.raytrain.yaml` 中配置 `save_path`** 或在提交时使用 **`--save-path`** 选项指向所有节点共享的存储（例如 MinIO 个人 bucket 或共享 NFS）：
    ```yaml
    # 在 .raytrain.yaml 中：
    save_path: s3://personal-bucket/exp/{user}/{run_id}
    ```
    或者命令行覆盖：
    ```bash
    raytrain submit --config configs/x.py --gpus 8 --nodes 2 \
        --save-path "s3://personal-bucket/exp/{user}/{run_id}"
    ```
    *注：`save_path` 模板中支持 `{user}`、`{run_id}`、`{config_name}` 占位符，框架在渲染时会自动替换。*
*   **运行中查看**：如果是本地路径，`raytrain exec <job_name>` 进 head pod，`ls /mnt/ray-cache/exp/...`；如果是 MinIO 路径，直接在个人 bucket 内实时查看。

## 5. 加载预训练权重

把权重提前上传到 MinIO，然后在 config 里引用 s3 路径（训练代码用 boto3 下载），
或者先 pull 到本地再传进去：

```bash
raytrain data pull s3://u-me-exp/<old-run>/model/model_best.pth \
                   /mnt/ray-cache/weights/model_best.pth

raytrain submit --config ... \
    --config-override weight=/mnt/ray-cache/weights/model_best.pth
```

## 6. GPU 类型选择

`--gpu-type h20` 和 `--gpu-type a100` 两选一。**单个任务必须在同类型 GPU 上**
（异构 GPU 的 NCCL 不稳定）。如果想"把所有 GPU 都吃完"，并发提交多个任务分别占不同池。

## 7. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `raytrain` 命令找不到 | CLI 没装 | `cd ~/raytrain && pip install -e .` |
| `manifest not found` | 不在项目根目录 | `cd` 到含 `.raytrain.yaml` 的目录 |
| Pod 一直 Pending | 该类型 GPU 不够 | `raytrain list --all-users` 看占用，排队等或换 gpu-type |
| "placement group not ready" | 同上 | 同上 |
| NCCL 第 0 步超时 | 网卡配置问题 | 找运维，大概率要调 `NCCL_SOCKET_IFNAME` |
| MLflow 显示 FAILED 但训练日志正常 | driver 后处理崩了 | `raytrain logs` 最后 50 行 |
| 想看 pod 里的文件 | — | `raytrain exec <job>` 进 pod |

## 8. 构建项目镜像（接入新训练仓库时）

当你有一个新的训练代码仓库想通过 raytrain 提交，需要构建一个项目镜像。

### 镜像体系

```
base-ray-pytorch（运维维护，已含 raytrain）
        │
        └─► 你的项目镜像（你自己构建）
```

base 镜像里已经装好了：CUDA + torch + ray + mlflow + boto3 + raytrain。
你的项目 Dockerfile **只需要装项目自己的依赖 + COPY 代码**。

### 项目 Dockerfile 模板

在你的项目仓库根目录创建 `Dockerfile`：

```dockerfile
FROM 172.31.9.104:5050/training/base-ray-pytorch:ray2.54.1-torch2.5.0-cu124

WORKDIR /workspace/<你的项目名>

# 1) 项目自己的 pip 依赖
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    <你项目需要的包>

# 2) 如果有 CUDA 扩展需要编译
COPY libs /workspace/<你的项目名>/libs
RUN if [ -d "./libs/xxx" ]; then pip install ./libs/xxx -v --no-build-isolation; fi

# 3) COPY 项目代码
COPY configs  /workspace/<你的项目名>/configs
COPY src      /workspace/<你的项目名>/src
COPY tools    /workspace/<你的项目名>/tools
COPY .raytrain.yaml /workspace/<你的项目名>/

# 自检（raytrain 已在 base 里，不用再装）
RUN python -c "import torch, ray, raytrain; print('OK')"
CMD ["bash"]
```

### 构建和推送

```bash
cd <你的项目根目录>
DOCKER_BUILDKIT=1 docker build \
    -t 172.31.9.104:5050/training/<项目名>:<tag> .
docker push 172.31.9.104:5050/training/<项目名>:<tag>
```

### 注意事项

1. **FROM 必须是 base-ray-pytorch**：这样自动带上 ray + raytrain，不用自己装。
2. **WORKDIR 要和 `.raytrain.yaml` 里的 `workdir:` 一致**。
3. **不要 `COPY . .`**：会把 `.git`、`data/`、`exp/` 等大目录拷进镜像。按需 COPY 具体子目录。
4. **CUDA 扩展放前面**：编译慢的层放前面，改代码时不会重编译（Docker 缓存）。
5. **项目代码放后面**：改代码频繁，放后面只重建最后几层。
6. **不需要装 raytrain**：base 里已经有了。
7. **镜像名字规范**：`172.31.9.104:5050/training/<项目名>:<tag>`。

### 构建完之后

1. 在项目根目录写 `.raytrain.yaml`（参考 `docs/adding-new-repo.md`）
2. 把 `.raytrain.yaml` 里的 `image:` 改成你刚推送的 tag
3. `raytrain submit --config ... --gpus ... --name ...`

### 常见构建问题

| 问题 | 解决 |
|---|---|
| `FROM base-ray-pytorch` 拉不到 | 确认内网 registry 地址和 tag 正确 |
| pip install 超时 | 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| CUDA 扩展编译失败 | 确认 `TORCH_CUDA_ARCH_LIST` 覆盖了目标 GPU 的 sm 版本 |
| 镜像太大 | 用 `.dockerignore` 排除 data / exp / .git 等 |
| 改代码后重建很慢 | 把 pip install 重依赖放前面，COPY 代码放后面，利用 Docker 层缓存 |

## 9. 代码同步与镜像（code-as-submission）

从 Phase 1 起，`raytrain submit` **默认会把当前工作目录打包成一个 zip 上传到
MinIO 的 `raytrain-code` bucket**，集群侧 Ray 通过 `runtime_env.working_dir`
自动拉取并解压、`chdir` 进去再跑训练。带来的直接好处：

> **改一行训练代码，直接 `raytrain submit` 重新提交即可，不用再 build / push 镜像。**

镜像现在只承载**环境**（CUDA / torch / ray / 项目依赖 + raytrain 本身），
**不再承载训练代码**。只有当你改了依赖（新增 pip 包、换 CUDA 版本等）时才需要
重建镜像。

下面六件事按你日常关心的顺序展开。

### 9.1 镜像现在只放环境，不放代码

| | 旧模式（code-in-image） | 现模式（code-as-submission，默认） |
|---|---|---|
| 镜像里有什么 | 环境 + 训练代码（`COPY` 进去） | **只有环境**（依赖 + raytrain） |
| 改一行代码 | build + push 整个镜像（4–8 GB） | `raytrain submit` 重新提交即可 |
| 代码怎么上集群 | 烧进镜像 | 打包 zip → MinIO → Ray `working_dir` 拉取 |
| 什么时候要重建镜像 | 改代码 / 改依赖都要 | **只有改依赖时** |

也就是说，提交流程多出"打包 + 上传代码"两个阶段，`raytrain submit` 会按编号
打印进度：

```text
[1/5] packaging code (workdir=/home/you/pointcept-main)
      excluded: .git/, data/, ...
      zip size: 87.3 MiB, sha256: a3f8c1d2e4b5...
[2/5] uploading code -> s3://raytrain-code/zhangsan/zhangsan-pointcept-smoke.zip
[3/5] creating MLflow run in experiment='pointcept'
[4/5] rendering RayJob (1×8 GPUs on h20)
[5/5] applying to namespace=ray-cluster-3
```

**回退到旧模式**：如果 working_dir 出问题，或者你确实想用烧进镜像的代码，
带 `--no-code-sync` 即可。此时跳过 `[1/5]`、`[2/5]` 两个阶段，行为与改造前
完全一致：

```bash
# 跳过打包上传，用镜像里烧进去的代码（旧行为）
raytrain submit --config configs/x.py --gpus 8 --nodes 1 --gpu-type h20 \
    --no-code-sync
```

其他相关选项：

| 选项 | 作用 |
|---|---|
| `--no-code-sync` | 跳过打包/上传，回到"镜像内代码"旧模式 |
| `--workdir-zip <path>` | 用一个**已经打好**的 zip，不再现场打包（仍会校验大小、算 hash、上传） |
| `--code-bucket <name>` | 覆盖默认 bucket `raytrain-code`（也可在 `.raytrain.yaml` 里设 `code_sync.bucket`） |

> 也可以在 `.raytrain.yaml` 里写 `code_sync.enabled: false` 永久关闭代码同步，
> 等价于每次都带 `--no-code-sync`。

### 9.2 working_dir 排除规则

打包时**不是**把整个目录原样塞进 zip——数据、checkpoint、`.git` 这些都会被排除。
排除规则按 **gitignore 语法**，优先级**从低到高（后者覆盖前者）**：

1. raytrain 内置默认规则（`DEFAULT_IGNORES`）
2. 仓库根的 `.gitignore`（如存在）
3. 仓库根的 `.raytrainignore`（如存在，见 9.3）
4. `.raytrain.yaml` 里的 `code_sync.extra_excludes`

内置默认规则覆盖了几类"几乎永远不该进 code zip"的东西：

| 类别 | 代表条目 |
|---|---|
| 版本控制 | `.git/`、`.hg/`、`.svn/` |
| Python 缓存 | `__pycache__/`、`*.pyc`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`*.egg-info/`、`build/`、`dist/` |
| 虚拟环境 | `.venv/`、`venv/`、`env/` |
| IDE / 编辑器 | `.idea/`、`.vscode/`、`*.swp` |
| 前端依赖 | `node_modules/` |
| **数据 / 权重 / 实验产物** | `data/`、`datasets/`、`exp/`、`outputs/`、`logs/`、`wandb/`、`*.ckpt`、`*.pth`、`*.pt`、`*.tar`、`*.safetensors` |
| 系统垃圾 | `.DS_Store`、`Thumbs.db` |
| raytrain 自身缓存 | `.raytrain-cache/` |

注意 `data/`、`datasets/`、`exp/`、`*.ckpt`、`*.pth` 已经在默认排除里，所以
**正常情况下你不用做任何配置**，数据和 checkpoint 都不会被打进 zip。

### 9.3 `.raytrainignore` 用法

如果默认规则没覆盖到你项目里的某些大文件 / 私有目录，在**仓库根**新建一个
`.raytrainignore` 文件即可，它会在默认规则和 `.gitignore` 之上追加排除项。

- 位置：仓库根目录（和 `.raytrain.yaml` 同级）
- 语法：与 `.gitignore` 完全一致（底层用 `pathspec`）
- 以 `#` 开头的行是注释；空行被忽略
- 仓库根已经带了一份 `.raytrainignore.example`，复制成 `.raytrainignore` 改即可：

```bash
cp .raytrainignore.example .raytrainignore
```

一个简单示例：

```gitignore
# .raytrainignore —— 语法同 .gitignore，在内置默认规则之上追加

# 项目特有的实验产物
outputs/
runs/
results/

# 不想上传的大文件
*.onnx
*.bin

# weights & biases / tensorboard
wandb/
events.out.tfevents.*
```

### 9.4 200 MiB 上限

单个 code zip 默认上限 **200 MiB**（由 `.raytrain.yaml` 里的
`code_sync.max_size_mib` 控制，默认 `200`）。**超限时 `raytrain submit` 会在
客户端直接报错并退出（非零退出码），根本不会上传**，同时打印 zip 中体积排名
**前 10 的文件**，方便你定位是谁把 zip 撑大的：

```text
code zip is 312.4 MiB, exceeds limit 200 MiB.

top-10 largest files in the bundle (consider adding to .raytrainignore):
  120.50 MiB  checkpoints/last.bin
   88.30 MiB  assets/demo.mp4
   ...
```

修复办法很直接：把这些文件加进 `.raytrainignore`（见 9.3），或者确认它们本就
该走数据通道（`datasets:` / `data_source:`，见第 4 节）而不是跟代码一起打包。
真有正当理由要更大的包，可在 `.raytrain.yaml` 里调高 `code_sync.max_size_mib`，
但通常不建议——大包会拖慢每次提交时 worker 拉取解压的速度。

```yaml
# .raytrain.yaml
code_sync:
  max_size_mib: 200          # 默认 200，可按需调整
  extra_excludes:
    - "outputs/"
    - "*.bin"
```

### 9.5 Code_Hash 与 MLflow tag

每次打包后，raytrain 会对 zip 内容算一个 **SHA256**（即 Code_Hash），用于版本
审计和复现：

- `raytrain submit` 终端会打印 hash 前 12 位（见 9.1 的 `[1/5]` 输出）。
- head pod 的 driver 启动日志里有一行可 grep 的
  `[driver] code_hash=<前12位>`（旧模式下为 `[driver] code_hash=<none>`），
  用来和提交端、MLflow tag 三方比对。
- 提交时会往 MLflow run 写三个 tag：

| MLflow tag | 含义 |
|---|---|
| `raytrain.code_uri` | code zip 的 s3 URI（`s3://raytrain-code/<user>/<job>.zip`） |
| `raytrain.code_hash` | code zip 的 SHA256 |
| `raytrain.code_size_bytes` | code zip 字节数 |

有了 `raytrain.code_uri`，事后就能根据某个 MLflow run 精确取回当时提交的那份
代码（配套的 `raytrain reproduce <mlflow_run_id>` 会用这个 tag 重新下载 zip）。

### 9.6 Code_Zip 7 天保留

`raytrain-code` bucket 配了 **7 天的对象生命周期（lifecycle）**：每个 code zip
在上传 7 天后会被 MinIO 自动删除。这意味着：

- **7 天内**的任意一次提交都能精确复现代码（zip 还在）。
- **超过 7 天**后，对应的 zip 已被清理，`raytrain reproduce` 无法再恢复那次的
  代码——这种长期回溯请依赖 **git commit**，提交前记得把代码 commit 一下。

也正因如此，临时上传失败的残留 zip 不用手动清，lifecycle 会兜底回收。bucket
名、lifecycle 配置、配额建议和紧急清理操作等运维细节见
[ops-guide.md 第 9 节「Code Bucket 运维」](ops-guide.md#9-code-bucket-运维)。
