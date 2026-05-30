# 新训练项目接入指南

一个新训练项目（不只是 Pointcept，任何 PyTorch DDP / torchrun / accelerate 项目）接入 raytrain，三步走。

## 全景

接入一个项目要完成：

1. **镜像 FROM `base-ray-pytorch`**（base 只有 raytrain 的第三方依赖，raytrain 源码由项目 Dockerfile 自己在最末层 COPY + install）
2. **在项目根目录写一份 `.raytrain.yaml`**（本文档的重点）
3. **数据上 MinIO 并在 yaml 里声明**（可选）

下面按顺序讲。

## 第 1 步 · 镜像

### 从 `base-ray-pytorch` 构建新项目镜像

base 镜像里已经装好了 ray、mlflow、boto3、minio、jinja2、click、pyyaml 等
raytrain 运行时需要的第三方依赖（但**不含 raytrain 源码本身**）。
新项目的 Dockerfile 负责 COPY 自己的训练代码 + 编译 CUDA 扩展 +
**在最末层 COPY raytrain 源码并 pip install**。

模板：

```dockerfile
FROM 172.31.9.104:5050/training/base-ray-pytorch:ray2.54.1-torch2.5.0-cu124

WORKDIR /workspace/<项目名>

# 1) 项目自己的重型依赖（放前面，缓存命中率高）
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    <你的项目需要的重型包>

# 2) 如果有 CUDA 扩展需要编译
COPY libs /workspace/<项目名>/libs
RUN if [ -d "./libs/xxx" ]; then pip install ./libs/xxx -v --no-build-isolation; fi

# 3) COPY 项目代码
COPY configs  /workspace/<项目名>/configs
COPY src      /workspace/<项目名>/src
COPY tools    /workspace/<项目名>/tools
COPY .raytrain.yaml /workspace/<项目名>/

# 自检（raytrain 已经在 base 里，不用再装）
RUN python -c "import torch, ray, raytrain; print('OK')"
CMD ["bash"]
```

**关键分层原则**：
| 层 | 何时失效 | 耗时 |
|---|---|---|
| L1 重型 pip 依赖（flash-attn 等） | 改版本时 | 10-60 分钟（首次） |
| L2 COPY libs + 编译 | 改 libs/ 时 | 2-5 分钟 |
| L3 COPY 训练代码 | 改 configs/ 或训练代码时 | 30 秒 |

构建推送：

```bash
docker build -f Dockerfile.<项目> \
    -t 172.31.9.104:5050/training/<项目>:<tag> .
docker push 172.31.9.104:5050/training/<项目>:<tag>
```

验证 raytrain 装进去了：

```bash
docker run --rm 172.31.9.104:5050/training/<项目>:<tag> \
    python -c "from raytrain.entrypoint import driver; print('OK')"
```

## 第 2 步 · 写 `.raytrain.yaml`

放在项目根目录。**最小示例**：

```yaml
apiVersion: raytrain/v1

image: 172.31.9.104:5050/training/<项目>:<tag>
workdir: /workspace/<项目>           # 镜像里代码所在目录
repo_name: <项目名>                  # MLflow experiment 名

launcher:
  type: native_ddp                  # 见下文"怎么选 launcher"
  entrypoint: tools/train.py
  args:
    - --config-file={config}
    - --num-gpus={num_gpus_per_node}
    - --num-machines={num_nodes}
    - --machine-rank={node_rank}
    - --dist-url=tcp://{master_addr}:{master_port}
    - --options
    - save_path={save_path}

resources:
  gpus_per_node: 8
  cpus_per_node: 32
  memory_per_node: 256Gi
  shm_size: 32Gi

# 数据集（可选）：框架会自动从 MinIO 下载到每个节点缓存并 symlink 到 workdir
# datasets:
#   - {name: my-ds, s3: s3://<bucket>/<prefix>, mount: data/my-ds}

# 额外 artifact 上传（可选）。save_path 目录会自动上传到 MLflow，这里只列加的。
artifacts:
```

### 模板里的占位符

`launcher.args` 里的 `{xxx}` 是占位符，driver 运行时会替换成真实值：

| 占位符 | 含义 |
|---|---|
| `{config}` | 用户 `--config` 传的路径 |
| `{num_gpus_per_node}` | 每节点 GPU 数 |
| `{num_nodes}` | 节点数 |
| `{node_rank}` | 当前节点 rank（0..N-1） |
| `{master_addr}` | rank-0 节点 IP |
| `{master_port}` | 自动选的空闲端口 |
| `{world_size}` | `num_nodes × num_gpus_per_node` |
| `{save_path}` | 实验输出保存目录。默认 `/mnt/ray-cache/exp/<user>/<run-id>`。可通过 `.raytrain.yaml` 的 `save_path` 或 CLI `--save-path` 自定义，支持 `{user}`、`{run_id}`、`{config_name}` 占位符。若指定为 `s3://` 等远程路径，则所有机器都会将输出写入该共享 MinIO 存储中，规避多机训练时不同机器写本地盘导致不可访问的 Bug。 |
| `{run_id}` | MLflow run id |
| `{workdir}` | `workdir` 字段值 |
| `{run_name}` | `<config_name>-<run_id前8位>`，常用于 Ray Train / MLflow run 名 |
| `{cpus_per_worker}` | `cpus_per_node / gpus_per_node`，给 Ray Train worker 设置合理 CPU 申请 |

### 怎么选 launcher

| launcher 类型 | 适用场景 |
|---|---|
| `native_ddp` | 项目有自己的入口脚本，接受 `--dist-url / --num-machines / --machine-rank / --num-gpus` 这类参数。框架只注入 `MASTER_ADDR/PORT/NODE_RANK/WORLD_SIZE` 然后直接跑 entrypoint。**Pointcept 用这个。** |
| `torchrun` | 代码里用 `torch.distributed.init_process_group()`，依赖 `torchrun` 设置 `RANK/LOCAL_RANK/WORLD_SIZE`。框架自动拼 `torchrun --nnodes=... --nproc_per_node=... --node_rank=... --master_addr=...` |
| `accelerate` | 用 HuggingFace `accelerate launch`。框架自动拼对应参数 |
| `custom` | 其他情况。`entrypoint` 原样作为命令前缀，`args` 原样附加 |
| `ray_train` | 训练入口内部使用 Ray Train / `TorchTrainer`。框架只在 head 启动一次 entrypoint，GPU worker 由训练代码里的 `TorchTrainer` 自己申请。**不要用 `custom` 替代。** |

### torchrun 示例

```yaml
launcher:
  type: torchrun
  entrypoint: train.py
  args:
    - --config={config}
    - --output-dir={save_path}
```

上面会被 driver 组装成：

```
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=0 \
    --master_addr=10.42.3.200 --master_port=29500 \
    train.py --config=configs/x.py --output-dir=/mnt/ray-cache/exp/...
```

### accelerate 示例

```yaml
launcher:
  type: accelerate
  entrypoint: train.py
  args:
    - --config={config}
```

### ray_train 示例

如果训练入口内部已经使用 `ray.train.torch.TorchTrainer`，例如：

```bash
python tools/train_ray.py --config configs/x.py --num-workers 8
```

那么 `.raytrain.yaml` 使用 `ray_train`：

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

`ray_train` 和其他 launcher 的最大区别：

- `native_ddp/torchrun/accelerate/custom`：raytrain 会创建每节点 GPU `NodeLauncher` actor，然后在 actor 里跑训练命令。
- `ray_train`：raytrain 不创建 GPU `NodeLauncher`，只在 head 里跑一次训练 driver；训练 driver 内部的 `TorchTrainer` 再向 Ray 集群申请 GPU worker。

如果把 `TorchTrainer` 项目放到 `native_ddp` 或 `custom` 中跑，GPU 可能先被父 actor 占住，内部 Ray Train worker 再申请 GPU 时会 pending。

## 第 3 步 · 数据集

数据有两种接入方式，选一种即可：

### 方式 A：预下载到本地（传统方式，小数据集适用）

在 `.raytrain.yaml` 声明 `datasets:`：

```yaml
datasets:
  - {name: my-ds, s3: s3://pointcept-data/my-ds, mount: data/my-ds}
```

- 首次启动自动从 MinIO 下载到节点 `/data4/ray-cache/datasets/my-ds/`
- workdir 下 `data/my-ds` 变成 symlink 指过去
- 后续任务命中缓存，跳过下载

**训练代码原样读 `data/my-ds/...`**，不用改一个字。

### 方式 B：Ray Data + Lance 流式读取（大数据集推荐）

数据提前用 Daft/Lance 转成 Lance 格式上传到 MinIO。在 `.raytrain.yaml` 用 `data_source:` 替代 `datasets:`：

```yaml
data_source:
  type: lance                                      # "lance" 或 "parquet"
  uri: s3://lance-datasets/nuscenes-v1/train.lance # MinIO 上的 Lance 路径
  version: latest                                  # Lance 版本号或 "latest"
  # filter: "split == 'train'"                     # 可选：过滤表达式
  # columns: [coord, strength, segment]            # 可选：列裁剪

cpu_workers: 4  # 可选：额外启动 4 个 CPU-only Pod 做数据预处理
```

> **`data_source:` 和 `datasets:` 互斥，二选一。**

**`cpu_workers` 字段说明：**

| 值 | 效果 |
|---|---|
| `0`（默认） | 不启动额外 Pod。数据预处理（decode/augment）在 GPU worker 的 CPU 上做 |
| `4` | 额外启动 4 个 **CPU-only Pod**（无 GPU），加入 Ray 集群，专门跑 Ray Data 的 `map_batches()` 预处理。GPU worker 只做训练，不做数据处理 |

这些 CPU Pod 的配置：
- `num-gpus: "0"` — 不占 GPU 资源
- `8 CPU / 32 GB RAM` — 足够跑 decode + augment
- 支持 autoscaling（`maxReplicas = cpu_workers × 2`）

训练代码需要用 `raytrain.data.RayLanceDataset` 或 `raytrain.data.auto_dataset()`。详见 `user-guide.md` 场景 D。

## 第 4 步 · 冒烟测试

先把 `datasets:` / `data_source:` 整段注释掉，跑最小 config：

```bash
cd <项目根>
raytrain submit --config <某个小 config> \
    --gpus 1 --nodes 1 --gpu-type h20 \
    --name smoke
raytrain logs <job-name> -f
```

看到 `[driver] node IPs: [...]` 接上正常训练输出，就算通了。

打开 `datasets:` 或 `data_source:` 再跑完整流程。

## 写 `.raytrainignore` 与镜像/代码依赖划分

从 Phase 1 起，`raytrain submit` **默认采用 code-as-submission**：提交时把当前
工作目录打包成 zip 上传到 MinIO 的 `raytrain-code` bucket，集群侧 Ray 通过
`runtime_env.working_dir` 自动拉取解压再跑训练。对接入新仓库来说，这带来两个
要提前想清楚的问题：**哪些文件该进 code zip（用 `.raytrainignore` 控制）**，
以及**哪些依赖该烧进镜像、哪些随 zip 走**。本节专门讲这两件事，完整机制见
[user-guide.md 第 9 节「代码同步与镜像」](user-guide.md#9-代码同步与镜像code-as-submission)。

### 怎么写 `.raytrainignore`

`.raytrainignore` 是放在**仓库根目录**（和 `.raytrain.yaml` 同级）的一个文件，
语法和 `.gitignore` 完全一致（底层用 `pathspec`）。它在 raytrain 内置默认规则
和 `.gitignore` 之上**追加**排除项，匹配到的文件不会被打进上传的 code zip。

仓库根已经带了一份 `.raytrainignore.example`，复制改即可：

```bash
cp .raytrainignore.example .raytrainignore
```

**大多数新仓库几乎不用配**，因为内置默认规则（`DEFAULT_IGNORES`）已经覆盖了
绝大多数"永远不该进 code zip"的东西：

| 类别 | 已默认排除的代表条目 |
|---|---|
| 版本控制 | `.git/`、`.hg/`、`.svn/` |
| Python 缓存 / 构建产物 | `__pycache__/`、`*.pyc`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`*.egg-info/`、`build/`、`dist/` |
| 虚拟环境 | `.venv/`、`venv/`、`env/` |
| IDE / 编辑器 | `.idea/`、`.vscode/`、`*.swp` |
| 前端依赖 | `node_modules/` |
| 数据 / 权重 / 实验产物 | `data/`、`datasets/`、`exp/`、`outputs/`、`logs/`、`wandb/`、`*.ckpt`、`*.pth`、`*.pt`、`*.tar`、`*.safetensors` |
| 系统垃圾 / raytrain 缓存 | `.DS_Store`、`Thumbs.db`、`.raytrain-cache/` |

注意 `build/`、`dist/`、`*.egg-info/` 这些**编译/打包产物已经在默认排除里**，
所以即使你的仓库有 `libs/` 下的 CUDA 扩展源码，它们的构建中间产物也不会跟着
打包（详见下一小节）。

新仓库**真正需要往 `.raytrainignore` 里加的**，是默认规则没覆盖、但又属于
本项目特有的大文件 / 私有目录，典型有三类：

- 项目特有的实验产物目录（比如不叫 `exp/outputs/` 而叫 `runs/`、`results/`）
- 不想随代码上传的大文件（`*.onnx`、`*.bin`、`*.parquet` 等）
- 本地缓存 / 自动生成的目录（`htmlcov/`、`docs/_build/`、`events.out.tfevents.*`）

一个具体示例：

```gitignore
# .raytrainignore —— 语法同 .gitignore，在内置默认规则之上追加

# 本项目特有的实验产物
runs/
results/

# 不想上传的大文件
*.onnx
*.bin
*.parquet

# 自动生成的文档 / 覆盖率报告
htmlcov/
docs/_build/

# tensorboard / wandb
events.out.tfevents.*
```

**200 MiB 上限**：单个 code zip 默认上限 200 MiB（由 `code_sync.max_size_mib`
控制）。超限时 `raytrain submit` 会在**客户端直接报错退出、根本不上传**，并打印
zip 里体积排名**前 10 的文件**，帮你定位是谁把包撑大的：

```text
code zip is 312.4 MiB, exceeds limit 200 MiB.

top-10 largest files in the bundle (consider adding to .raytrainignore):
  120.50 MiB  checkpoints/last.bin
   88.30 MiB  assets/demo.mp4
   ...
```

按这个 top-10 列表把对应文件加进 `.raytrainignore` 即可；如果它们本就属于数据，
应该走 `datasets:` / `data_source:`（见第 3 步）而不是跟代码一起打包。

### 怎么选保留在镜像里的依赖

判断标准只有一句话：**装得慢 / 改得少的进镜像，改得勤的随 zip 走。**

| 归属 | 放什么 | 为什么 |
|---|---|---|
| **进镜像**（Dockerfile） | 重型 pip 依赖（如 flash-attn）、编译型 CUDA 扩展（`libs/` 下的 pointops / 自定义算子）、系统库、conda 环境、**raytrain 本身** | 安装/编译慢、很少变；烧进镜像后每次提交都能复用，无需重装 |
| **随 code zip 走**（working_dir） | 训练代码（`tools/`、`src/`）、`configs/`、小脚本、`.raytrain.yaml` | 改得勤；走 zip 后改一行直接 `raytrain submit` 重提，不用重建镜像 |

也就是说，**接入新仓库时，镜像的职责只剩"环境"，不再是"环境 + 代码"**。
对照第 1 步的 Dockerfile 模板：依赖安装和 CUDA 扩展编译要照常做，但
**第 3 步那几行 `COPY configs/src/tools/.raytrain.yaml` 在 code-as-submission
模式下不再是必需的**——这些代码会在提交时随 zip 上集群。

> 第 1 步的 Dockerfile 仍然 `COPY` 了训练代码，这是为了兼容 `--no-code-sync`
> 回退路径（用镜像里烧进去的代码）。默认 code-as-submission 模式下这些 `COPY`
> 层不会被用到，新仓库如果确定只走默认模式，可以省掉它们，只保留依赖安装 +
> CUDA 扩展编译 + 末层 `pip install raytrain`。

关于编译型扩展有一个容易混淆的点：`libs/` 下的 CUDA 扩展**源码**可能仍然在
仓库里、会被打进 zip，但真正被 import 的是**镜像里编译好的已安装产物**。所以
不用担心扩展"没编译"，也不用把编译中间产物塞进 zip——`build/`、`dist/`、
`*.egg-info/` 已经在默认排除里了，正常情况下无需额外配置。

什么时候才需要重建镜像？**只有改了依赖时**——新增/升级 pip 包、换 CUDA 版本、
改 CUDA 扩展源码（需要重新编译）。只改训练代码 / config，直接 `raytrain submit`
即可，不用碰镜像。

## `.raytrain.yaml` 完整字段参考

```yaml
apiVersion: raytrain/v1                     # 必填，固定值

image: registry/project:tag                 # 必填，训练镜像
workdir: /workspace/project                 # 必填，镜像内代码目录
repo_name: project                          # 可选，MLflow experiment 名

launcher:                                   # 必填
  type: native_ddp | torchrun | accelerate | custom | ray_train
  entrypoint: tools/train.py                # 相对 workdir 的入口脚本
  args: [...]                               # 支持 {config} 等占位符
  env: {}                                   # 额外环境变量

resources:                                  # 可选，有默认值
  gpus_per_node: 8
  cpus_per_node: 32
  memory_per_node: 256Gi
  shm_size: 32Gi

# --- 数据：二选一 ---
datasets:                                   # 方式 A：预下载
  - {name: ds, s3: s3://..., mount: data/ds}

data_source:                                # 方式 B：Ray Data 流式读取
  type: lance                               #   "lance" | "parquet"
  uri: s3://lance-datasets/xxx.lance        #   MinIO 上的数据路径
  version: latest                           #   版本号
  filter: ""                                #   过滤表达式
  columns: []                               #   列裁剪

cpu_workers: 0                              # CPU-only Pod 数量 (配合 data_source)

save_path: ""                               # 可选，默认为本地 NVMe '/mnt/ray-cache/exp/{user}/{run_id}'。
                                            # 多机训练推荐指向共享存储，如 's3://personal-bucket/exp/{user}/{run_id}'
                                            # 或本地共享 NFS 目录。

artifacts:                                  # 可选
  - /mnt/ray-cache/exp
```

## 接入 Checklist

- [ ] 镜像 push 到内网 registry，`docker run ... python -c "from raytrain.entrypoint import driver"` 能通过
- [ ] `.raytrain.yaml` 提交到项目仓库根目录
- [ ] 目标节点已经打过 `gpu=a100` 或 `gpu=h20` 标签
- [ ] 冒烟测试：单卡单机跑 `--config` 小配置，10 分钟内通过
- [ ] 数据集（如有）已上传 MinIO，开启 `datasets:` 或 `data_source:` 后仍跑通
- [ ] MLflow 的 `<repo_name>` experiment 下能看到 run 和 artifact

## 常见坑

- `workdir:` 必须和 Dockerfile 里 `COPY` 目标路径一致（如 `/workspace/pointcept`）。
- `launcher.entrypoint` 是**相对 workdir** 的路径。
- CUDA 扩展（pointops / pointgroup_ops 这种）**必须**在构建镜像时就编译好，不能放 entrypoint 里现场装。
- `.raytrain.yaml` 的 `datasets[].mount` 是**相对 workdir** 的路径。
- `data_source:` 和 `datasets:` **不能同时使用**。
- `cpu_workers` 只在 `data_source:` 模式下有意义。不用 `data_source:` 时设成 0。
- code-as-submission 默认开启：**镜像只装依赖、不放代码**（除非用 `--no-code-sync` 回退到镜像内代码）；大文件 / 产物记得加进 `.raytrainignore`，否则可能撞上 200 MiB 上限。
