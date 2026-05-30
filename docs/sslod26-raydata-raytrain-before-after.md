# sslod26-master 到 sslod26-main：Ray Data/Lance 训练改造说明

这份文档专门说明 `sslod26-main` 是如何在 `sslod26-master` 基础上改造成 Ray Data/Lance 训练链路的。这里的“改造前后”指的是：

```text
改造前：sslod26-master 原始 Pointcept/sslod26 训练链路
改造后：sslod26-main 已跑通的 Ray Train + Ray Data + Lance 训练链路
```

不是指本轮我对代码做了哪些 diff。本文最后单独附了一节“本轮为接入当前 raytrain 做的框架改造”，用于说明 `raytrain` 新增了哪些能力。

## 1. 一句话总结

`sslod26-main` 的核心改造是：把 `sslod26-master` 原来依赖 Pointcept 原生 Dataset/DataLoader/DDP 的训练路径，改成 **Ray Train 负责分布式训练进程，Ray Data 负责从 Lance/MinIO 读取点云数据，Pointcept 只保留模型、transform、optimizer、scheduler、hooks 和训练循环**。

改造后的主链路是：

```text
Lance metadata
  -> Ray Data read_lance
  -> filter(port, split)
  -> 根据 lidar_path 拉 S3/MinIO .bin 点云
  -> decode 成 coord / strength
  -> 可选写 parquet cache
  -> Ray Data streaming_split 做 rank 分片
  -> Pointcept transform + point_collate_fn
  -> RayTrainer
  -> Sonata SSL model
```

这条链路主要服务 **Sonata SSL 预训练**，不依赖 `segment` 标签。

## 2. 改造前：sslod26-master 原始训练方式

### 2.1 启动入口

原始训练入口是：

```text
tools/train.py
```

典型启动命令是：

```bash
python tools/train.py \
  --config-file configs/sonata/pretrain-sonata-v1m1-0-base.py \
  --num-gpus 8 \
  --num-machines 1 \
  --machine-rank 0 \
  --dist-url tcp://<master>:<port>
```

### 2.2 分布式方式

`tools/train.py` 会调用 Pointcept 的 launch 逻辑：

```text
tools/train.py
  -> pointcept/engines/launch.py
  -> torch.multiprocessing.spawn
  -> 每张 GPU 一个本地进程
  -> torch.distributed.init_process_group("NCCL")
  -> Pointcept Trainer
```

也就是说，改造前的分布式训练由 Pointcept 自己管理：

- 自己 spawn 训练进程。
- 自己初始化 process group。
- 自己设置 local rank / global rank。
- 自己构建 `DistributedSampler`。
- 自己构建 PyTorch `DataLoader`。

### 2.3 数据读取方式

改造前是标准 Pointcept Dataset/DataLoader 模型：

```text
Pointcept config
  -> build_dataset(cfg.data.train)
  -> Dataset.__getitem__
  -> transform
  -> torch DataLoader
  -> point_collate_fn
  -> Trainer.run_step
```

数据通常来自本地目录、预处理后的 `.npy`、`.pkl` 或项目内置 dataset 格式。训练代码默认假设数据已经在容器本地路径里，例如：

```text
data/scannet
data/nuscenes
/data4/ray-cache/datasets/...
```

### 2.4 改造前的限制

这套方式适合中小数据集或已经预处理并同步到本地盘的数据，但对当前大规模 OCC/Lance 场景有几个问题：

- 数据量大时，先把全量数据同步到每个 GPU 节点代价高。
- 训练开始前要等数据下载和解压。
- 对 MinIO/S3 上的 Lance metadata 和原始 `.bin` 点云没有天然支持。
- Dataset/DataLoader worker 只在本训练进程内工作，不能利用 Ray Data 的分布式读和预处理能力。
- 多机训练的数据切分依赖 PyTorch sampler，不适合直接消费 Ray Data shard。

## 3. 改造后：sslod26-main 的 Ray Data 训练方式

### 3.1 新启动入口

改造后新增入口：

```text
tools/train_ray.py
```

典型命令是：

```bash
python tools/train_ray.py \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --num-workers 8 \
  --run-name sslod26-demo
```

这里的 `--num-workers` 对应 Ray Train worker 数，通常等于总 GPU 数。

### 3.2 新分布式方式

改造后不再让 Pointcept `mp.spawn` 创建训练进程，而是用 Ray Train：

```text
tools/train_ray.py
  -> ray.init(address=auto)
  -> TorchTrainer
  -> Ray Train 创建 num_workers 个 worker
  -> Ray Train 初始化 torch.distributed
  -> 每个 worker 内运行 train_loop_per_worker
```

Ray Train 负责：

- 分配 worker actor。
- 设置 `RANK`、`LOCAL_RANK`、`WORLD_SIZE`、`LOCAL_WORLD_SIZE`。
- 初始化 torch distributed process group。
- 设置 GPU device。
- 管理 worker failure/retry。

Pointcept 不再负责创建分布式进程，只在每个 Ray Train worker 内执行训练逻辑。

### 3.3 新数据读取方式

改造后的数据读取由 `pointcept/datasets/occ_lance.py` 提供。

核心数据流：

```text
ray.data.read_lance(LANCE_URI)
  -> filter port == "cnezhhh" and split == "train"
  -> map_batches(fetch_lidar)
  -> map_batches(decode_lidar)
  -> write_parquet/read_parquet cache
  -> streaming_split(world, equal=True)[rank]
  -> OccLanceShardIter
  -> Pointcept transform
  -> point_collate_fn
```

这里 Lance 表不是直接训练样本本体，而是 metadata 表。关键字段包括：

```text
port
split
scene_token
sample_token
lidar_path
```

真正点云数据在 `lidar_path` 指向的位置：

```text
s3://occ-data/<port>/<package>/samples/LIDAR_TOP/<timestamp>.bin
```

`fetch_lidar()` 从 MinIO/S3 拉取 `.bin` 字节，`decode_lidar()` 将其转成模型输入：

```python
coord = points[:, :3].astype(np.float32)
strength = (points[:, 3:4] / 255.0).astype(np.float32)
```

### 3.4 新训练器

改造后新增：

```text
pointcept/engines/train_ray.py
```

其中定义：

```python
@TRAINERS.register_module("RayTrainer")
class RayTrainer(Trainer):
    def build_train_loader(self):
        return self.cfg._ray_train_shard
```

它只改一件事：跳过原来的 `build_dataset + DataLoader`，直接使用 `tools/train_ray.py` 注入的 Ray Data shard。

其他训练能力仍然复用 Pointcept：

- model 构建
- DDP wrapper
- optimizer
- scheduler
- AMP
- hooks
- checkpoint
- logging

### 3.5 新 config

改造后新增：

```text
configs/sonata/pretrain-sonata-occ-lance-demo.py
```

它从 Sonata 原始预训练配置改来，核心变化是：

- `train.type = "RayTrainer"`
- `model.backbone.in_channels = 4`
- 输入从 indoor 数据的 xyz/normal/color 变成 lidar 的 xyz/intensity
- transform 去掉 color/normal 相关增强
- `data.train.type = "OccLanceDataset"`
- SSL 预训练不使用 `segment`

注意：Ray Train 主路径下，`data.train` 不是实际 DataLoader 来源，而是保留给 config 初始化、transform 和兼容测试使用。真正的训练 batch 来自 `cfg._ray_train_shard`。

## 4. 文件级改造对照

### 4.1 tools/train.py -> tools/train_ray.py

改造前：

```text
tools/train.py
```

职责：

- 解析 Pointcept config。
- 调用 Pointcept launch。
- 由 Pointcept 自己 spawn 多进程。

改造后新增：

```text
tools/train_ray.py
```

职责：

- 连接 Ray 集群。
- 创建或复用 MLflow run。
- 在 driver 端准备 Ray Data cache。
- 创建 `TorchTrainer`。
- 在每个 Ray Train worker 内：
  - 加载 Pointcept config。
  - 构建 Ray Data dataset。
  - `streaming_split(world)[rank]` 做分片。
  - 构造 `OccLanceShardIter`。
  - 注入 `cfg._ray_train_shard`。
  - 构建 `RayTrainer` 并开始训练。

可以这样介绍：

> `tools/train_ray.py` 是 Ray Train 版训练入口，它替代了原来的 Pointcept launch/mp.spawn。它本身不直接训练一个进程，而是创建 `TorchTrainer`，由 Ray Train 根据资源启动多个 worker，每个 worker 再进入 Pointcept Trainer。

### 4.2 原 Dataset -> pointcept/datasets/occ_lance.py

改造前：

```text
Pointcept Dataset
  -> __getitem__
  -> local file
  -> transform
  -> DataLoader
```

改造后新增：

```text
pointcept/datasets/occ_lance.py
```

关键函数/类：

| 名称 | 作用 |
|---|---|
| `_read_lance_filtered()` | 用 Ray Data 读 Lance metadata，并按 `port/split` 过滤 |
| `fetch_lidar()` | 根据 `lidar_path` 从 S3/MinIO 拉 `.bin` 原始点云 |
| `decode_lidar()` | 将 `.bin` 解码为 `coord` 和 `strength` |
| `build_demo_ray_dataset()` | 串起 Lance 读取、fetch、decode、cache，返回 Ray Dataset |
| `OccLanceShardIter` | 把 Ray Data shard 包装成 Pointcept Trainer 可迭代对象 |
| `OccLanceDataset` | 轻量 torch Dataset facade，只用于隔离测试/兼容注册 |

可以这样介绍：

> `occ_lance.py` 不再是传统意义上每个 worker 单独读文件的 Dataset，而是 Ray Data pipeline 的封装。它把 Lance metadata、MinIO 点云对象、decode、cache 和 Ray shard 组织成训练可消费的数据流。

### 4.3 DefaultTrainer -> RayTrainer

改造前：

```text
Trainer.build_train_loader()
  -> build_dataset(cfg.data.train)
  -> DistributedSampler
  -> torch DataLoader
```

改造后：

```text
RayTrainer.build_train_loader()
  -> return cfg._ray_train_shard
```

为什么这样做：

- Ray Data shard 已经按 rank 切好了。
- 不应该再套 `DistributedSampler`。
- 不应该再开 DataLoader worker。
- 不应该把 Ray Data batch 再交给 PyTorch DataLoader 去拉。

可以这样介绍：

> RayTrainer 是最小侵入改造。我们没有重写 Pointcept 训练循环，只替换了 train loader 来源，让 Trainer 直接消费 Ray Data shard。

### 4.4 launch.py 新增 setup_ray_local_group

改造前：

Pointcept 原生 DDP 会在 `_distributed_worker()` 里创建 local process group，供 `comm.get_local_rank()` 使用。

改造后：

Ray Train 已经初始化了全局 process group，但不会帮 Pointcept 创建 `_LOCAL_PROCESS_GROUP`。所以新增：

```python
setup_ray_local_group()
```

职责：

- 读取 Ray Train 注入的 `LOCAL_WORLD_SIZE`。
- 按每台机器的 local ranks 创建 subgroup。
- 写入 `pointcept.utils.comm._LOCAL_PROCESS_GROUP`。

可以这样介绍：

> 这是 Ray Train 和 Pointcept DDP 包装之间的适配层。Ray Train 已经有分布式环境，但 Pointcept 的 DDP wrapper 还需要 local process group，所以要补一次。

### 4.5 原 Sonata config -> occ-lance demo config

改造前：

```text
configs/sonata/pretrain-sonata-v1m1-0-base.py
```

面向 indoor/pretrain 数据，输入通常包含 xyz、normal、color 等。

改造后：

```text
configs/sonata/pretrain-sonata-occ-lance-demo.py
```

关键变化：

| 项 | 改造前 | 改造后 |
|---|---|---|
| trainer | `DefaultTrainer` | `RayTrainer` |
| input channel | indoor 多模态特征 | `coord + strength`，4 通道 |
| dataset | 原 Pointcept dataset | `OccLanceDataset` 占位/兼容，实际走 Ray shard |
| transform | color/normal/indoor 增强 | lidar 几何增强 |
| label | 视任务而定 | SSL 预训练不需要 `segment` |
| evaluation | 可评估 | demo 默认不评估 |

## 5. 改造前后链路对比表

| 维度 | sslod26-master 改造前 | sslod26-main 改造后 |
|---|---|---|
| 训练入口 | `tools/train.py` | `tools/train_ray.py` |
| 分布式启动 | Pointcept `mp.spawn` | Ray Train `TorchTrainer` |
| process group | Pointcept 初始化 | Ray Train 初始化，Pointcept 补 local group |
| 数据来源 | 本地 Dataset 文件 | Lance metadata + MinIO/S3 点云对象 |
| 数据读取 | PyTorch Dataset/DataLoader | Ray Data pipeline |
| 数据分片 | `DistributedSampler` | `streaming_split(world)[rank]` |
| batch 组装 | DataLoader + collate | Ray shard -> transform -> `point_collate_fn` |
| trainer | `DefaultTrainer` | `RayTrainer` |
| 标签要求 | 取决于任务 | SSL 不需要 `segment` |
| 缓存 | 本地数据缓存/预处理 | decoded parquet cache |
| 提交方式 | 手工命令或原生 DDP | 手写 RayJob，后续接入 `raytrain submit` |

## 6. 为什么这是“真正训练”，不是只读数据

只把 Ray Data 读出来不等于训练跑通。真正训练必须满足：

1. Ray Data 能读到 Lance metadata。
2. 能根据 `lidar_path` 拉到真实 `.bin`。
3. `.bin` 能 decode 成模型需要的 `coord/strength`。
4. 每个 rank 拿到不同 shard。
5. 每个 sample 进入 Pointcept 原有 transform。
6. batch 经过 `point_collate_fn` 变成模型期望格式。
7. `RayTrainer` 能进入 Pointcept `run_step()`。
8. 模型能 forward/backward/optimizer step。

`sslod26-main` 的意义就是把这 8 个环节串通了。

## 7. SSL 预训练和监督语义分割必须分开讲

### SSL 预训练

`sslod26-main` 当前跑通的是 SSL 预训练。

它需要：

```text
coord
strength
name/sample_token
```

它不需要：

```text
segment
lidar_semseg_path
```

所以即使 Lance metadata 中 `lidar_semseg_path=nan`，SSL 预训练也可以继续跑。

### 监督语义分割

监督语义分割需要 label。

它需要：

```text
coord
strength
segment
name
```

或者能从 metadata 中的标签路径解码出 `segment`。

如果 `lidar_semseg_path=nan`，那不能认为监督分割训练已经可用。它只是说明 SSL 预训练路径可用。

可以这样介绍：

> sslod26-main 当前验证的是 Ray Data 读 Lance 做 SSL 预训练，不是验证所有有监督任务都已经 Lance 化。有监督任务还要补标签读取和 schema 适配。

## 8. sslod26-main 为什么一开始用手写 RayJob

`sslod26-main` 里有：

```text
deploy/rayjob-sslod26-demo.yaml
```

这是为了先绕过 raytrain，直接验证 Ray Train/Ray Data 训练链路。手写 RayJob 负责：

- 启动 RayCluster。
- 指定 head/worker 镜像。
- 设置 GPU worker 数。
- 注入 MinIO/Lance/NCCL 环境变量。
- 执行 `python tools/train_ray.py ...`。

也就是说，`sslod26-main` 先证明训练代码本身可跑；后续再把 RayJob 生成、资源、镜像、MLflow、提交统一交给 `raytrain`。

## 9. 接入 raytrain 后的目标形态

手写 RayJob 目标替换为：

```bash
raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 8 \
  --nodes 1 \
  --gpu-type h20 \
  --name sslod26-raydata
```

`.raytrain.yaml` 使用：

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

data_source:
  type: lance
  uri: s3://occ-lance/nuscenes_v1
```

执行模型变成：

```text
raytrain submit
  -> 创建 MLflow run
  -> 渲染 RayJob/ConfigMap/Secret
  -> RayJob head 启动 raytrain.entrypoint.driver
  -> driver 发现 launcher.type=ray_train
  -> driver 不创建 GPU NodeLauncher
  -> driver 在 head 中启动一次 tools/train_ray.py
  -> tools/train_ray.py 创建 TorchTrainer
  -> TorchTrainer 创建 GPU workers
  -> workers 用 Ray Data 读 Lance 训练
```

## 10. 本轮为接入当前 raytrain 做的框架改造

这一节才是“我这轮对当前 `pointcept-main/raytrain` 做了什么”。它和上面的 `sslod26-master -> sslod26-main` 代码改造是两件事。

### 10.1 新增 launcher.type = ray_train

当前通用 raytrain 已支持：

```text
native_ddp
torchrun
accelerate
custom
ray_train
```

新增 `ray_train` 是为了支持内部已经使用 Ray Train/TorchTrainer 的训练入口。

### 10.2 raytrain/manifest.py

改动：

- `SUPPORTED_LAUNCHERS` 增加 `ray_train`。
- `.raytrain.yaml` 可以合法声明 `launcher.type: ray_train`。

### 10.3 raytrain/entrypoint/launchers.py

改动：

- 新增 `ray_train()` command builder。
- 支持 `{world_size}`、`{run_name}`、`{save_path}`、`{cpus_per_worker}` 等占位符。

### 10.4 raytrain/entrypoint/driver.py

改动：

- 新增 driver-side launcher 分支。
- `ray_train` 不创建 placement group。
- `ray_train` 不创建 GPU `NodeLauncher` actor。
- 只在 head 中启动一次 entrypoint。
- 由训练入口内部的 `TorchTrainer` 申请 GPU worker。

这避免了：

```text
父 NodeLauncher 占 GPU
  -> 子 TorchTrainer 再申请 GPU
  -> 资源冲突 / pending
```

### 10.5 raytrain/rayjob.py

改动：

- 将 `data_source` 注入 Ray pod 环境。
- 将 `launcher.env` 注入 Ray pod 环境。
- 增加 `MINIO_ACCESS_KEY/MINIO_SECRET_KEY` secret alias。

原因：Ray Train worker 是新的 Ray actor，不是 driver 直接 fork 出来的进程，所以 worker 也必须拿到 MinIO、Lance、NCCL、cache 等环境变量。

### 10.6 raytrain/templates/rayjob.yaml.j2

改动：

- NCCL 默认值只有在 manifest 没设置时才补。
- `.raytrain.yaml launcher.env` 中的 `NCCL_SOCKET_IFNAME` 可以覆盖模板默认值。

### 10.7 公共文档同步

已同步四份公共文档：

```text
docs/quickstart.md
docs/user-guide.md
docs/adding-new-repo.md
docs/ops-guide.md
```

并新增/重写这份独立说明文档：

```text
docs/sslod26-raydata-raytrain-before-after.md
```

## 11. 如何对外完整介绍这件事

可以按下面这个顺序讲。

### 第一步：先讲为什么要改

> 原来的 sslod26-master 走 Pointcept 原生 DDP 和 PyTorch Dataset/DataLoader，适合本地数据或已经同步到节点的数据。但现在数据在 MinIO 上，并且组织成 Lance metadata + 原始 lidar bin 的形式，全量同步到本地不现实，所以要用 Ray Data 流式读取和预处理。

### 第二步：讲训练入口怎么变

> 原来入口是 `tools/train.py`，由 Pointcept 自己 `mp.spawn` 多进程。改造后新增 `tools/train_ray.py`，由 Ray Train 的 `TorchTrainer` 创建多 GPU worker。Pointcept 不再负责启动分布式进程，只在每个 Ray Train worker 里执行训练。

### 第三步：讲数据怎么变

> 原来 Dataset 从本地文件读样本。现在先用 `ray.data.read_lance` 读 metadata，再根据 `lidar_path` 到 MinIO 拉 `.bin`，decode 成 `coord/strength`，写 parquet cache，然后每个 rank 用 `streaming_split` 拿自己的 shard。

### 第四步：讲 Pointcept 怎么保留

> 我们没有重写模型训练。Pointcept 的模型、transform、collate、optimizer、scheduler、hooks 都保留。只新增 `RayTrainer`，让 `build_train_loader()` 直接返回 Ray Data shard。

### 第五步：讲为什么 raytrain 也要改

> raytrain 原来的 `native_ddp` 会先创建 GPU NodeLauncher actor，再在 actor 里跑训练脚本。这适合 `tools/train.py`，但不适合 `tools/train_ray.py`，因为后者内部还要创建 TorchTrainer worker。如果父 actor 先占住 GPU，内部 worker 再申请 GPU 就会冲突。所以 raytrain 新增 `ray_train`，只在 head 启动一次 Ray Train driver，让 TorchTrainer 自己调度 GPU。

### 第六步：讲当前验证边界

> 当前验证的是 Sonata SSL 预训练链路，不需要 `segment` 标签。它证明了 Ray Data/Lance 读点云、分片、transform、collate、训练 step 这条链路能跑。监督语义分割还需要标签路径或 `segment` schema 进一步接入。

## 12. 最终效果

改造完成后，训练提交从手写 RayJob 变成：

```bash
raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 1 \
  --nodes 1 \
  --gpu-type h20 \
  --name sslod26-raydata-smoke
```

单卡通过后扩展到：

```bash
raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 8 \
  --nodes 2 \
  --gpu-type h20 \
  --name sslod26-raydata-2node
```

目标验收标准：

- RayJob 提交成功。
- 日志出现 `launcher type ray_train`。
- 日志出现 `tools/train_ray.py` 启动。
- Ray Data 能读取 Lance。
- 能根据 `lidar_path` 拉取并 decode 点云。
- TorchTrainer worker 正常启动。
- 每个 rank 拿到自己的 shard。
- Pointcept transform/collate 正常执行。
- 训练进入第一个 step。
- MLflow run、checkpoint、日志路径可查。

## 13. 记忆版总结

可以记成三句话：

1. `sslod26-master` 原来是 Pointcept 原生 DDP + 本地 Dataset；`sslod26-main` 改成 Ray Train + Ray Data + Lance。
2. 数据不再全量同步本地，而是 Lance metadata 指向 MinIO 点云，Ray Data 拉取、decode、cache、分片。
3. 接入 raytrain 时不能用 `native_ddp/custom` 包 `tools/train_ray.py`，必须用新增 `ray_train`，让 TorchTrainer 自己申请 GPU worker。
