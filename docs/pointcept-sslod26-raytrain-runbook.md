# Pointcept 与 sslod26 使用 raytrain 提交训练完整手册

这份文档说明两个项目如何通过当前 `raytrain submit` 提交 KubeRay RayJob，并使用 Ray Data/Lance 从 MinIO 读取数据。

两个项目虽然都基于 Pointcept 代码体系，但提交方式不是同一条链路：

| 项目 | 推荐训练目标 | launcher | 训练入口 | 数据路径 |
|---|---|---|---|---|
| `pointcept-main` | 监督语义分割 | `native_ddp` | `tools/train.py` | `PointceptRayLanceDataset` 通过 Ray Data 读 Lance |
| `sslod26-master` | Sonata SSL 预训练 | `ray_train` | `tools/train_ray.py` | `TorchTrainer + Ray Data + OccLanceShardIter` |

核心原则：

- Pointcept 监督训练继续用原生 `tools/train.py`，由 Pointcept 自己 `mp.spawn` / DDP。
- sslod26 SSL 预训练用 `tools/train_ray.py`，由 Ray Train `TorchTrainer` 自己创建 GPU worker。
- `tools/train_ray.py` 不能放进 `native_ddp` 的 `NodeLauncher` 里跑，否则外层 actor 先占 GPU，内层 TorchTrainer 再申请 GPU 时会冲突。
- `.raytrain.yaml` 里的 `data_source:` 和 `datasets:` 互斥。使用 Ray Data/Lance 时只保留 `data_source:`。

## 1. 前置检查

### 1.1 本地目录

当前约定目录：

```bash
/Users/ashersu/Desktop/go-project/pointcept-main
/Users/ashersu/Desktop/go-project/sslod26-master
/Users/ashersu/Desktop/go-project/pointcept-main/raytrain
```

其中：

- `pointcept-main/raytrain` 是当前要使用和维护的 `raytrain`。
- `sslod26-master/raytrain` 如果存在，只当历史副本看待，不作为最终适配目标。

### 1.2 raytrain CLI

在提交节点或 Kasm 环境里确认 CLI 可用：

```bash
raytrain --help
raytrain list
```

如果是第一次使用，需要先配置 MinIO、MLflow、namespace 等信息：

```bash
raytrain configure
```

### 1.3 当前 raytrain 必须支持的 launcher

当前 `raytrain` 应该支持：

```text
native_ddp
torchrun
accelerate
custom
ray_train
```

重点检查 `ray_train` 是否存在。它是 sslod26 的 `tools/train_ray.py` 必需能力。

可以在镜像或本地 Python 环境里检查：

```bash
python - <<'PY'
from raytrain.entrypoint.launchers import LAUNCHERS
print(sorted(LAUNCHERS))
assert "native_ddp" in LAUNCHERS
assert "ray_train" in LAUNCHERS
PY
```

如果没有 `ray_train`，说明 CLI 或镜像里的 `raytrain` 版本旧，需要更新当前 `raytrain` 或重建 base 镜像。

## 2. Pointcept 项目：监督语义分割提交流程

### 2.1 代码目录

```bash
cd /Users/ashersu/Desktop/go-project/pointcept-main
```

关键文件：

| 文件 | 作用 |
|---|---|
| `.raytrain.yaml` | raytrain 提交规范 |
| `dockerfile/Dockerfile.pointcept` | Pointcept 镜像构建文件 |
| `configs/nuscenes/semseg-pt-v3m1-0-lance.py` | Lance 监督训练配置 |
| `pointcept/datasets/ray_lance.py` | `PointceptRayLanceDataset` |
| `tools/train.py` | Pointcept 原生训练入口 |

### 2.2 `.raytrain.yaml` 关键形态

Pointcept 使用 `native_ddp`：

```yaml
image: 172.31.9.104:5050/training/pointcept:ray2.54.1-torch2.5.0-cu124-raydata2.0
workdir: /workspace/pointcept
repo_name: pointcept
save_path: s3://guofeng-su-workspace/exp/{user}/{run_id}

launcher:
  type: native_ddp
  entrypoint: tools/train.py
  env:
    PYTHONPATH: /workspace/pointcept:/opt/conda/lib/python3.11/site-packages
    WANDB_MODE: "disabled"
  args:
    - --config-file={config}
    - --num-gpus={num_gpus_per_node}
    - --num-machines={num_nodes}
    - --machine-rank={node_rank}
    - --dist-url=tcp://{master_addr}:{master_port}
    - --options
    - save_path={save_path}

data_source:
  type: lance
  uri: s3://occ-lance/nuscenes_v1
  version: latest
```

注意：

- `save_path` 不需要在提交命令里写 `--save-path`。
- Pointcept 原生入口通过 `--options save_path=...` 覆盖配置。
- Lance URI 要指向包含 `_versions` 的 Lance dataset 根目录。这里是 `s3://occ-lance/nuscenes_v1`，不是 `s3://occ-lance/nuscenes_v1/data`。

### 2.3 构建 Pointcept 镜像

在 `pointcept-main` 根目录执行：

```bash
cd /Users/ashersu/Desktop/go-project/pointcept-main

export POINTCEPT_IMAGE=172.31.9.104:5050/training/pointcept:ray2.54.1-torch2.5.0-cu124-raydata2.0

DOCKER_BUILDKIT=1 docker build \
  -f dockerfile/Dockerfile.pointcept \
  -t ${POINTCEPT_IMAGE} \
  .

docker push ${POINTCEPT_IMAGE}
```

如果内网 pip 源需要显式指定：

```bash
DOCKER_BUILDKIT=1 docker build \
  -f dockerfile/Dockerfile.pointcept \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
  -t ${POINTCEPT_IMAGE} \
  .
```

### 2.4 构建后镜像自检

```bash
docker run --rm ${POINTCEPT_IMAGE} python - <<'PY'
import torch, ray, raytrain
from raytrain.entrypoint.launchers import LAUNCHERS
from pointcept.datasets.ray_lance import PointceptRayLanceDataset
print("torch:", torch.__version__)
print("ray:", ray.__version__)
print("launchers:", sorted(LAUNCHERS))
assert "native_ddp" in LAUNCHERS
print("Pointcept image OK")
PY
```

如果这里 `import raytrain` 失败，说明 base 镜像或项目镜像没有安装 raytrain。

如果 `PointceptRayLanceDataset` import 失败，说明项目代码没有被正确 COPY 进镜像，或镜像 tag 不是刚构建的 tag。

### 2.5 更新或覆盖镜像 tag

推荐把 `.raytrain.yaml` 的 `image:` 更新成刚 push 的 tag。

如果只是临时测试，也可以提交时覆盖：

```bash
raytrain submit \
  --image ${POINTCEPT_IMAGE} \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 1 --nodes 1 --gpu-type h20 --name point-lance-smoke --dry-run
```

### 2.6 dry-run

```bash
cd /Users/ashersu/Desktop/go-project/pointcept-main

raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 1 \
  --nodes 1 \
  --gpu-type h20 \
  --name point-lance-smoke \
  --dry-run
```

dry-run 重点看：

- `image` 是刚构建的 Pointcept 镜像。
- `workdir` 是 `/workspace/pointcept`。
- `launcher.type` 是 `native_ddp`。
- entrypoint 最终是 `tools/train.py`。
- 参数里有 `--config-file=configs/nuscenes/semseg-pt-v3m1-0-lance.py`。
- 参数里有 `--options save_path=...`。
- manifest 里有 `data_source`。
- 日志或 manifest 显示跳过 `datasets` 本地同步。

### 2.7 单卡 smoke

```bash
raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 1 \
  --nodes 1 \
  --gpu-type h20 \
  --name point-lance-smoke
```

提交成功后会输出 job name。后续用这个 job name 查日志：

```bash
raytrain logs <job_name> -f
```

期望看到：

```text
[driver] data_source mode: lance @ s3://occ-lance/nuscenes_v1 (skipping dataset_sync)
[node0] launching in /workspace/pointcept: python tools/train.py ...
[PointceptRayLanceDataset] read_lance uri=s3://occ-lance/nuscenes_v1 ...
Starting execution of Dataset ...
Train: [1/...
```

进入第一个 `Train:` step 后，说明提交、镜像、RayJob、Ray Data、Pointcept DDP 主链路已经跑通。

### 2.8 单节点多卡

单卡 smoke 通过后，再跑 8 卡：

```bash
raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 8 \
  --nodes 1 \
  --gpu-type h20 \
  --name point-lance-1node
```

检查点：

- 日志中 `--num-gpus=8`。
- `WORLD_SIZE` 是 8。
- 每个 local rank 正常初始化。
- Ray Data 不重复阻塞。
- GPU 都有显存占用。

### 2.9 多节点

单节点 8 卡通过后，再扩到 2 节点：

```bash
raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 8 \
  --nodes 2 \
  --gpu-type h20 \
  --name point-lance-2node
```

检查点：

- `--num-machines=2`。
- `--machine-rank=0/1` 分别出现在不同节点。
- `dist-url=tcp://<master_addr>:<master_port>` 对所有节点一致。
- 多节点 NCCL 初始化成功。
- 每个 rank 的 Ray/Lance 分片没有重复或空转。

### 2.10 Pointcept 训练结果和验收

训练产物主要由 Pointcept 写入 `save_path`：

```text
s3://guofeng-su-workspace/exp/{user}/{run_id}
```

通常包括：

- checkpoint，例如 `model_last.pth`、`model_best.pth` 或项目配置里定义的权重文件。
- TensorBoard event。
- 训练日志。
- MLflow run 记录。

重要判断：

- 只看到 `Train:` step，表示链路跑通。
- 如果 loss 长期是 `0.0000`，不一定是真正有效监督训练。Pointcept 监督训练需要有效 `segment` 标签或可解码的 `lidar_semseg_path`。
- 当前 nuScenes Lance 如果 `lidar_semseg_path=nan` 或 label 都是 ignore，监督 loss 可能为 0。这说明训练 pipeline 在跑，但数据标签还不满足真正监督语义分割验收。
- 真正监督训练验收要看 label schema、有效类别分布、loss 是否非零、mIoU/val 是否能跑。

## 3. sslod26 项目：Sonata SSL 预训练提交流程

### 3.1 代码目录

```bash
cd /Users/ashersu/Desktop/go-project/sslod26-master
```

关键文件：

| 文件 | 作用 |
|---|---|
| `.raytrain.yaml` | raytrain 提交规范 |
| `Dockerfile` | sslod26 镜像构建文件 |
| `tools/train_ray.py` | Ray Train driver |
| `pointcept/datasets/occ_lance.py` | Ray Data/Lance metadata 读取、S3 点云拉取、bin 解码 |
| `pointcept/engines/train_ray.py` | `RayTrainer`，把 Ray Data shard 接入 Pointcept Trainer |
| `configs/sonata/pretrain-sonata-occ-lance-demo.py` | Sonata SSL 预训练配置 |

### 3.2 `.raytrain.yaml` 关键形态

sslod26 使用 `ray_train`：

```yaml
image: 172.31.9.104:5050/training/sslod26:raytrain-raytrainer-v1
workdir: /workspace/pointcept
repo_name: sslod26
save_path: s3://occ-checkpoints/sslod26-raytrain/{user}/{run_id}

launcher:
  type: ray_train
  entrypoint: tools/train_ray.py
  env:
    PYTHONPATH: /workspace/pointcept:/opt/conda/lib/python3.11/site-packages
    OCC_LANCE_CACHE: s3://occ-checkpoints/sslod26-demo-cache
    NCCL_IB_DISABLE: "1"
    NCCL_SOCKET_IFNAME: eth0
    GLOO_SOCKET_IFNAME: eth0
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
  version: latest
```

注意：

- `ray_train` 模式只在 RayJob head 中启动一次 `tools/train_ray.py`。
- GPU worker 由 `tools/train_ray.py` 内部的 `TorchTrainer` 申请。
- `{world_size}` 会展开为 `nodes * gpus_per_node`。
- `{cpus_per_worker}` 会按 `cpus_per_node / gpus_per_node` 计算。
- SSL 预训练不依赖 `segment` 标签，`lidar_semseg_path=nan` 不影响 SSL 路径。

### 3.3 构建前检查 Dockerfile

当前 base 镜像会持续保持最新 `raytrain` 时，项目镜像最好不要再安装陈旧的 `raytrain` 副本。

如果 `sslod26-master/Dockerfile` 里存在：

```dockerfile
COPY raytrain /tmp/raytrain
RUN pip install -e /tmp/raytrain
```

要二选一：

1. 推荐：删除这两行，让镜像直接使用 base 镜像里的最新 `raytrain`。
2. 或者：确保 `sslod26-master/raytrain` 与当前 `/Users/ashersu/Desktop/go-project/pointcept-main/raytrain` 完全同步。

否则最常见问题是：提交侧支持 `ray_train`，但训练镜像里被旧副本覆盖，RayJob 运行时报 `unsupported launcher: ray_train` 或找不到 `ray_train`。

### 3.4 构建 sslod26 镜像

```bash
cd /Users/ashersu/Desktop/go-project/sslod26-master

export SSLOD_IMAGE=172.31.9.104:5050/training/sslod26:raytrain-raytrainer-v1

DOCKER_BUILDKIT=1 docker build \
  -f Dockerfile \
  -t ${SSLOD_IMAGE} \
  .

docker push ${SSLOD_IMAGE}
```

### 3.5 构建后镜像自检

```bash
docker run --rm ${SSLOD_IMAGE} python - <<'PY'
import torch, ray, raytrain
from raytrain.entrypoint.launchers import LAUNCHERS
from pointcept.datasets.occ_lance import OccLanceShardIter
from pointcept.engines.train_ray import RayTrainer
print("torch:", torch.__version__)
print("ray:", ray.__version__)
print("launchers:", sorted(LAUNCHERS))
assert "ray_train" in LAUNCHERS
print("sslod26 image OK")
PY
```

如果 `ray_train` 不在 `LAUNCHERS` 里，优先检查 Dockerfile 是否安装了旧的 `raytrain` 副本。

### 3.6 dry-run

```bash
cd /Users/ashersu/Desktop/go-project/sslod26-master

raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 1 \
  --nodes 1 \
  --gpu-type h20 \
  --name sslod-ssl-smoke \
  --dry-run
```

dry-run 重点看：

- `image` 是刚构建的 sslod26 镜像。
- `workdir` 是 `/workspace/pointcept`。
- `launcher.type` 是 `ray_train`。
- entrypoint 是 `tools/train_ray.py`。
- 参数里有 `--num-workers 1`。
- 参数里有 `--cpus-per-worker 32` 或符合当前资源设置的值。
- manifest 里有 `data_source.uri: s3://occ-lance/nuscenes_v1`。
- 不触发 `datasets` 本地同步。

### 3.7 单卡 smoke

```bash
raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 1 \
  --nodes 1 \
  --gpu-type h20 \
  --name sslod-ssl-smoke
```

查看日志：

```bash
raytrain logs <job_name> -f
```

期望看到：

```text
[driver] data_source mode: lance @ s3://occ-lance/nuscenes_v1 (skipping dataset_sync)
[ray-train] launching in /workspace/pointcept: python tools/train_ray.py ...
TorchTrainer ...
ray.data.read_lance ...
OccLanceShardIter ...
Train: ...
```

进入第一个训练 step 后，说明 SSL 预训练链路跑通：

```text
Lance metadata -> Ray Data -> S3 lidar_path -> decode bin -> Ray Data shard -> RayTrainer -> Sonata model
```

### 3.8 单节点多卡

```bash
raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 8 \
  --nodes 1 \
  --gpu-type h20 \
  --name sslod-ssl-1node
```

检查点：

- `--num-workers 8`。
- TorchTrainer 启动 8 个 worker。
- 每个 worker 有 rank 日志。
- `streaming_split` 分片正常。
- 没有 GPU pending。
- 没有 Ray Data deadlock。

### 3.9 多节点

```bash
raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 8 \
  --nodes 2 \
  --gpu-type h20 \
  --name sslod-ssl-2node
```

检查点：

- `--num-workers 16`。
- TorchTrainer worker 分布到两个节点。
- NCCL/GLOO 正常初始化。
- Ray Data shard 不重复、不阻塞。
- checkpoint 只由合理 rank 写入。

### 3.10 sslod26 训练结果和验收

训练产物写入：

```text
s3://occ-checkpoints/sslod26-raytrain/{user}/{run_id}
```

通常包括：

- Ray Train/TorchTrainer checkpoint。
- Pointcept/SSL 配置保存的模型权重。
- 训练日志。
- MLflow run 记录。
- 可能还有 `OCC_LANCE_CACHE` 指向的 parquet/cache 中间产物。

SSL 路径的验收标准：

- 日志出现 `ray.data.read_lance`。
- 日志出现 S3 `lidar_path` fetch/decode。
- TorchTrainer worker 正常启动。
- 训练进入第一个 step。
- 多卡时每个 rank 都有数据。
- checkpoint/log 能写到 `save_path`。

## 4. 通用调试命令

### 4.1 查看任务

```bash
raytrain list
raytrain list --all-users
```

### 4.2 查看日志

```bash
raytrain logs <job_name> -f
raytrain logs <job_name> --worker 0 -f
```

### 4.3 进入 Pod

```bash
raytrain exec <job_name>
raytrain exec <job_name> --worker 0
```

进入后常用：

```bash
pwd
ls -lah
env | sort | grep -E 'RAYTRAIN|RAY|AWS|MINIO|MLFLOW|NCCL|GLOO'
python -c "import raytrain; from raytrain.entrypoint.launchers import LAUNCHERS; print(sorted(LAUNCHERS))"
python -c "import ray, torch; print(ray.__version__, torch.__version__)"
nvidia-smi
ray status
```

### 4.4 停止任务

```bash
raytrain stop <job_name>
```

smoke 测试只要确认进入第一个 step，就可以停止，避免占用 GPU。

### 4.5 查看 MLflow

```bash
raytrain mlflow <job_name>
raytrain mlflow <job_name> --open
```

## 5. dry-run 检查清单

每次改镜像、改 `.raytrain.yaml`、改 launcher 后，先 dry-run。

Pointcept dry-run 要看到：

| 检查项 | 期望 |
|---|---|
| `launcher.type` | `native_ddp` |
| entrypoint | `tools/train.py` |
| config 参数 | `--config-file=...semseg-pt-v3m1-0-lance.py` |
| DDP 参数 | `--num-gpus`、`--num-machines`、`--machine-rank`、`--dist-url` |
| save path | `--options save_path=...` |
| data source | `s3://occ-lance/nuscenes_v1` |
| dataset sync | 跳过 |

sslod26 dry-run 要看到：

| 检查项 | 期望 |
|---|---|
| `launcher.type` | `ray_train` |
| entrypoint | `tools/train_ray.py` |
| worker 数 | `--num-workers = nodes * gpus` |
| CPU 数 | `--cpus-per-worker = cpus_per_node / gpus_per_node` |
| save path | `--storage-path ...` |
| data source | `s3://occ-lance/nuscenes_v1` |
| dataset sync | 跳过 |

## 6. 常见问题和处理

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `unsupported launcher: ray_train` | 镜像或提交端 raytrain 版本旧 | 更新当前 `raytrain`，重建 base 或项目镜像；检查是否被 `sslod26-master/raytrain` 旧副本覆盖 |
| RayJob 启动后 `tools/train_ray.py` pending | 错用了 `native_ddp/custom` 跑 Ray Train 入口 | sslod26 必须使用 `launcher.type: ray_train` |
| `Dataset at path nuscenes_v1/data was not found` | Lance URI 指到了非 dataset 根目录 | 改成包含 `_versions` 的根路径，例如 `s3://occ-lance/nuscenes_v1` |
| `KeyError: coord` | 读到的是 metadata Lance，不是直接点云 tensor schema | Pointcept 监督路径需要 `PointceptRayLanceDataset` 能从 `lidar_path` 解码点云和标签；SSL 路径使用 `occ_lance.py` |
| `lidar_semseg_path=nan` | 当前 Lance metadata 没有监督标签路径 | SSL 预训练不受影响；监督语义分割需要补 `segment` 或有效 `lidar_semseg_path` |
| `ValueError: output array is read-only` | Ray Data/Numpy 返回只读数组，Pointcept transform 原地改数组 | 进入 transform 前 `.copy()`；确认镜像包含修复后的代码 |
| loss 一直 `0.0000` | 标签缺失或全是 ignore label | 说明训练链路在跑，但不代表监督训练有效；检查 label schema 和类别分布 |
| `No module named pointcept` | `workdir` 或 `PYTHONPATH` 不对 | 确认 Dockerfile COPY 到 `/workspace/pointcept`，`.raytrain.yaml` 的 `workdir/PYTHONPATH` 与之匹配 |
| NCCL 初始化失败 | 网卡名、跨节点网络、IB 设置不匹配 | H20 当前优先设置 `NCCL_IB_DISABLE=1`、`NCCL_SOCKET_IFNAME=eth0`、`GLOO_SOCKET_IFNAME=eth0` |
| object store memory warning | Ray object store 比例偏低 | 先不作为失败处理；吞吐不够再调大 `object_store_memory` |
| 任务一直 Pending | GPU 资源不足、节点池不匹配、镜像拉取慢 | `raytrain list`、`kubectl describe pod` 或联系集群侧看资源 |

## 7. 推荐发布节奏

两个项目都按同样节奏推进：

1. 构建镜像并 push。
2. 镜像内执行 import 自检。
3. 更新 `.raytrain.yaml image` 或提交时用 `--image` 覆盖。
4. `--dry-run` 检查 RayJob manifest。
5. 单卡 smoke，进入第一个 step 即可停止。
6. 单节点多卡。
7. 多节点。
8. 查看 MLflow 和 `save_path` 产物。

Pointcept 命令汇总：

```bash
cd /Users/ashersu/Desktop/go-project/pointcept-main

raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 1 --nodes 1 --gpu-type h20 --name point-lance-smoke --dry-run

raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 1 --nodes 1 --gpu-type h20 --name point-lance-smoke

raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 8 --nodes 1 --gpu-type h20 --name point-lance-1node

raytrain submit \
  --config configs/nuscenes/semseg-pt-v3m1-0-lance.py \
  --gpus 8 --nodes 2 --gpu-type h20 --name point-lance-2node
```

sslod26 命令汇总：

```bash
cd /Users/ashersu/Desktop/go-project/sslod26-master

raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 1 --nodes 1 --gpu-type h20 --name sslod-ssl-smoke --dry-run

raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 1 --nodes 1 --gpu-type h20 --name sslod-ssl-smoke

raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 8 --nodes 1 --gpu-type h20 --name sslod-ssl-1node

raytrain submit \
  --config configs/sonata/pretrain-sonata-occ-lance-demo.py \
  --gpus 8 --nodes 2 --gpu-type h20 --name sslod-ssl-2node
```

## 8. 对外说明口径

可以这样介绍：

> 我们把两个 Pointcept 系项目拆成两种提交模式。Pointcept 监督语义分割仍然走原生 DDP，所以 raytrain 用 `native_ddp` 每个节点启动一次 `tools/train.py`，训练代码内部自己 `mp.spawn`。数据侧不再本地同步数据集，而是通过 `data_source` 注入 Lance URI，由 `PointceptRayLanceDataset` 使用 Ray Data 从 MinIO/Lance 读取。

> sslod26 的 Sonata SSL 预训练已经改造成 Ray Train 训练入口，入口是 `tools/train_ray.py`。这个入口内部会创建 `TorchTrainer` 和 Ray Data pipeline，所以 raytrain 新增 `ray_train` launcher，只在 RayJob head 里启动一次 driver，不提前占用 GPU。GPU worker 由 TorchTrainer 自己调度。

> 两个项目的提交体验保持一致，都是 `raytrain submit --config ... --gpus ... --nodes ...`。差异被收敛到各自 `.raytrain.yaml` 里：Pointcept 是 `native_ddp`，sslod26 是 `ray_train`。

