# raytrain 训练平台改造方案 v3

| 项目 | raytrain Platform |
|---|---|
| 文档状态 | **Draft v3，待评审** |
| 作者 | raytrain 维护者 |
| 评审对象 | 团队 leader / 平台同事 / 算法用户代表 |
| 当前版本 | 2026-05 |
| 主要变更 | v3 在 v2 基础上：聚焦"Ray Data + Lance 零拷贝"为平台核心数据通路；新增 Dataset 注册表（用户可注册）；新增 `ray_data_lance` launcher；明确个人/公共 MinIO bucket 区分 |

---

## 目录

1. [一句话讲清](#1-一句话讲清)
2. [背景与现状](#2-背景与现状)
3. [目标与非目标](#3-目标与非目标)
4. [产品形态：用户旅程](#4-产品形态用户旅程)
5. [核心概念模型](#5-核心概念模型)
6. [Ray Data + Lance 零拷贝架构](#6-ray-data--lance-零拷贝架构)
7. [总体架构](#7-总体架构)
8. [关键设计决策](#8-关键设计决策)
9. [路线图（7 个 Milestone）](#9-路线图)
10. [Milestone 详细任务](#10-milestone-详细任务)
11. [数据模型与 API 契约](#11-数据模型与-api-契约)
12. [部署与运维](#12-部署与运维)
13. [风险评估](#13-风险评估)
14. [团队与时间](#14-团队与时间)
15. [待确认事项](#15-待确认事项)

---

## 1. 一句话讲清

把 raytrain 升级成一个**完全在浏览器里使用的训练平台**，平台**首要支持"Ray Data + Lance 零拷贝"训练范式**。

> **用户在自己电脑上只装一个浏览器**。在平台里申请开发机（自带 Jupyter / VS Code / PyCharm / SSH 四种 IDE）→ 在开发机里写代码、调试 → 选好 Lance 数据集 → 一键提交训练 → 在平台里看任务、看日志、看结果。

用户**完全不需要**：
- ❌ 本机装 Python / raytrain CLI / Docker / kubectl
- ❌ 配置 kubeconfig
- ❌ 登录 Kasm
- ❌ 把数据下载到本地盘

平台**默认数据通路**：Lance（MinIO）→ Ray Data（read_lance + ActorPool）→ Plasma 缓存（超出 spill 到本地盘）→ 训练子进程零拷贝消费。

---

## 2. 背景与现状

### 2.1 当前架构（简化）

```
用户机器（必须 Kasm）       集群侧
─────────────────         ─────────────────
.raytrain.yaml             K8s（kubeconfig per-user）
~/.kube/config                    │
   │                              ▼
   ▼                       KubeRay → per-job RayCluster
raytrain CLI ─kubectl→     image: 含训练代码（每改重 build）
                                  │
                                  ▼
                          MinIO + MLflow + Lance datasets
```

数据通路（已有，工作良好）：

```
Lance dataset (MinIO: s3://occ-lance/...)
       │
       ▼ read_lance(zero-copy)
Ray Data (block-level shuffle, ActorPool transform)
       │
       ▼ Plasma cache → ray-spill (本地盘 /mnt/ray-spill)
       │
       ▼ iter_torch_batches (zero-copy Arrow → Tensor)
训练子进程
```

### 2.2 痛点

| 维度 | 问题 |
|---|---|
| 开发效率 | 改一行代码 → docker build / push / submit，单循环 5–30 分钟 |
| 入门门槛 | 必须登录 Kasm；新人配置环境耗时 |
| 凭据安全 | 每人持有 kubeconfig，泄露面广 |
| 数据可发现性 | Lance 数据集靠口口相传 / 内部文档；没有统一目录 |
| 资源管理 | 配额只到 namespace 粒度，无法做"每人 N 卡" |
| 可见性 | 只能 CLI `raytrain list` |
| 调试体验 | Kasm + raytrain exec 体验割裂 |

### 2.3 已具备的基础设施

| 组件 | 状态 |
|---|---|
| K8s（RKE2 v1.34） | ✅ |
| KubeRay Operator | ✅ |
| MinIO（个人 + 公共 bucket） | ✅ |
| MLflow | ✅ |
| Longhorn（含 RWX share-manager） | ✅ |
| NGINX Ingress + cert-manager | ✅ |
| GPU Operator + DCGM | ✅ |
| GitLab（内部） | ✅ |
| Prometheus + Grafana + Loki | ✅ |
| **Ray Data + Lance 零拷贝代码（raytrain.data 模块）** | ✅ |

**结论**：底层完整。本次方案做"上层应用 + 把现有 Lance 通路标准化"。

---

## 3. 目标与非目标

### 3.1 目标（Goals）

#### 用户侧

- **G1**：仅需浏览器即可使用平台全部功能
- **G2**：申请 Workspace（长寿 CPU 开发机），几十秒内可用
- **G3**：4 种 IDE（Jupyter / VS Code / PyCharm / SSH）任选
- **G4**：在 IDE / UI 里一键提交训练，不 build 镜像
- **G5**：申请短期 GPU DevSession 调试，与 Workspace 共享代码（PVC）
- **G6**：**浏览 / 注册 / 选用 Lance 数据集**，不再口口相传
- **G7**：训练默认走 Ray Data + Lance 零拷贝通路

#### 管理员侧

- **G8**：用户 / 租户 / 配额 / 镜像 / 数据集白名单管理
- **G9**：每用户 / 每团队 GPU、Workspace、Job 上限可配
- **G10**：审计日志 + 监控大盘

#### 工程侧

- **G11**：完全复用现有 K8s + KubeRay + MinIO + MLflow + Longhorn + GitLab
- **G12**：现有 `.raytrain.yaml` + Lance 训练代码不需要重写
- **G13**：raytrain `RayLanceDataset` + transform 库作为平台标准库分发

### 3.2 非目标（Non-Goals）

- ❌ 替换 KubeRay / MLflow / Longhorn
- ❌ 自研对象存储 / 元数据库
- ❌ 多集群 / 跨地域调度
- ❌ 流式推理 / 在线服务
- ❌ 计费 / 财务核算
- ❌ 模型注册表（继续用 MLflow Model Registry）
- ❌ HPO / Pipeline / DAG 编排（v1 只提供入口，不内置）
- ❌ 用户本机装 raytrain CLI（仅作为兜底，不是常规用法）

---

## 4. 产品形态：用户旅程

### 4.1 普通用户日常

```
1. 浏览器登录 https://raytrain.example.com
        │
        ▼
2. 进入"我的工作区" → 创建 Workspace（首次）
        - 选模板镜像（点云训练 / NLP / 通用）
        - 选大小（4C8G / 8C16G）
        - PVC（默认 100Gi）
        │
        ▼ 几十秒
3. Workspace Running → 4 IDE 入口任选
        ├─ "Jupyter Lab"     → 浏览器新页
        ├─ "VS Code"         → 浏览器新页（code-server）
        ├─ "PyCharm"         → 浏览器新页（Projector）
        └─ "复制 SSH 命令"
        │
        ▼ 在 IDE 里
4. git clone / git pull（连内部 GitLab）
        │
        ▼ Jupyter cell 里调试
5. 直接 ray.data.read_lance("s3://occ-lance/nuscenes_v1") 看数据
        - Workspace 已注入 MinIO 凭据
        - pylance / pyarrow / ray[data] 已预装
        │
        ▼ 想用 GPU 试跑
6. UI 申请 DevSession（1 张 H20）
        - 自动挂同一 PVC
        - 同样 4 IDE 入口
        - 同一份代码
        │
        ▼ 调试通过
7. 提交训练（两种入口等价）：
        a) UI 提交页：选 Workspace + 选 Dataset（下拉）+ 选 GPU 数 → 提交
        b) Workspace 终端：raytrain submit ...
        │
        ▼
8. UI 任务列表实时刷新 → 详情看日志（SSE）→ 完成跳 MLflow
```

### 4.2 数据集注册流程（新）

```
1. 数据 owner（普通用户即可）UI 进"数据集"页
        │
        ▼
2. 点"注册数据集"
        - name: my-pretrain-data
        - type: lance
        - uri: s3://my-bucket/pretrain.lance
        - visibility: private | tenant | public
        - tags: [point-cloud, lidar]
        │
        ▼
3. 平台后端用 lance metadata reader 自动扫
        - schema、rows、size_bytes、versions
        │
        ▼
4. 数据集出现在列表，可被任何有权限的用户在提交训练时下拉选用
```

### 4.3 管理员日常

```
- 管用户：增删改、配额、暂停
- 管租户：GPU 上限、Workspace 上限、Job 并发
- 管资源：长寿 cluster 容量、Workspace 数、DevSession 数、节点维护
- 管镜像：白名单（Workspace / DevSession / Job 三类）
- 管数据集：审核 public 数据集、清理孤儿数据集
- 看审计：全量、按用户/时段筛选
```

---

## 5. 核心概念模型

```
Tenant（租户/团队）─────────── 资源边界
   │
   ├─ User
   │    ├─ Workspace（CPU 长寿开发机）
   │    │      └─ DevSession（GPU 短期会话，挂同 PVC）
   │    │
   │    ├─ Job（训练任务，提交到长寿 cluster）
   │    │
   │    ├─ Token（身份凭证）
   │    │
   │    └─ Dataset（用户注册的数据集，private/tenant/public）
   │
   └─ Resource Quota：GPU / Workspace / Job

Cluster（长寿 RayCluster，按 GPU 类型分）
   ├─ ray-shared-h20
   └─ ray-shared-a100

Image Registry（admin 维护的镜像白名单）
   ├─ Workspace 镜像（CPU + 4 IDE）
   ├─ DevSession 镜像（GPU + 4 IDE + 训练依赖）
   └─ Job 镜像（GPU + 训练依赖，瘦身：不含训练代码）

Public Dataset Registry（admin 审核）
   └─ 跨 tenant 可见的数据集
```

### 5.1 Workspace（长寿 CPU 开发机）

不带 GPU；写代码、git、跑小测试用。

资源：1× CPU pod（4–32 vCPU）+ 用户独占 PVC（100Gi 默认）+ MinIO 凭据
入口：Jupyter / VS Code / PyCharm / SSH 全装
状态：running / stopped / archived
寿命：用户主动删；30 天无活动自动 stop

### 5.2 DevSession（短期 GPU 调试会话）

绑到一个 Workspace；用完即弃。

资源：1× GPU pod（1–8 卡）
存储：自动挂关联 Workspace 的 PVC（同代码视图）
镜像：训练镜像（pylance / ray[data] / 项目依赖）
寿命：4h 无心跳回收，最长 24h

### 5.3 Job（训练任务）

提交到长寿 RayCluster，跑完即归档。

资源：N × GPU 节点（多机多卡）
代码：来自 Workspace 当前目录的 zip 快照（runtime_env.working_dir）
数据：来自 Dataset（read_lance via Ray Data）
产物：checkpoint → MinIO，metrics → MLflow

### 5.4 Dataset（数据集注册）

平台一等公民。**专为 Lance + Ray Data 通路服务**，但兼容其他类型。

```yaml
Dataset:
  name: nuscenes-v1-train             # 全局唯一（在 visibility 范围内）
  type: lance | parquet | dir         # v1 主推 lance
  uri: s3://my-bucket/nuscenes.lance  # 用户指定，不绑 bucket
  version: latest | "3" | ...
  visibility: private | tenant | public
  schema: [...]                       # 平台扫 metadata 自动填
  rows: 28000                         # 自动填
  size_bytes: 1234567890              # 自动填
  owner: zhangsan
  tenant: occ-team
  tags: [point-cloud, lidar, nuscenes]
  description: |
    nuScenes v1 训练集 ...
```

**关键设计**：
- URI 用户自由指定，平台**不绑死任何 bucket**（兼容 `pointcept-data` / `occ-lance` / 个人 bucket）
- 平台只是"索引 + 元数据 + 权限"层，**不复制数据**
- 数据访问凭据走用户/租户的 MinIO bucket policy（个人 bucket 用户自己有权限；公共 bucket admin 配 policy）

---

## 6. Ray Data + Lance 零拷贝架构

这是平台的"灵魂层"。单独详写。

### 6.1 数据通路（不变，但平台标准化）

```
┌──────────────────────────────────────────┐
│ MinIO（Lance 数据集）                       │
│  s3://occ-lance/nuscenes_v1               │
│  s3://u-zhangsan-data/my-pretrain.lance   │
│  s3://pointcept-data/scannet              │
└─────────────────┬────────────────────────┘
                  │ read_lance (zero-copy)
                  ▼
┌──────────────────────────────────────────┐
│ Ray Data Pipeline                         │
│  ds = ray.data.read_lance(uri, ...)       │
│  ds = ds.randomize_block_order()          │
│  ds = ds.map_batches(MyTransform,         │
│        compute=ActorPoolStrategy(...))    │
│  if multi_epoch:                          │
│      ds = ds.materialize()  → Plasma      │
└─────────────────┬────────────────────────┘
                  │ DDP shard
                  ▼
       ds.split(world_size)[rank]
                  │
                  ▼
┌──────────────────────────────────────────┐
│ iter_torch_batches                        │
│  - prefetch_batches=4                     │
│  - local_shuffle_buffer_size=512          │
│  - zero-copy Arrow → Tensor               │
└─────────────────┬────────────────────────┘
                  ▼
              训练子进程

[支撑层]
  Plasma object store     ←→  /mnt/ray-spill (hostPath, 本地盘)
                              超出内存自动 spill
```

### 6.2 平台对 Lance 的支持等级

```
Level 0: 用户自己写 ray.data.read_lance(uri)             ← 现状
Level 1: 平台 Dataset 注册表，用户 select dataset_id     ← M3 加
Level 2: 标准 transform 库（点云 / 图像 / 文本等）        ← M4 加
Level 3: Job 模板：选模型类型 → 自动生成 pipeline        ← v2 后续
```

v1 (M0–M6) 做到 Level 2。

### 6.3 raytrain.data 模块作为平台标准库

```python
# 平台保证 Workspace 镜像 / DevSession 镜像 / Job 镜像里都装这个模块
from raytrain.data import auto_dataset
from raytrain.data.transforms.pointcloud import VoxelizeActor, RandomFlip

# 一行代码：从 RAYTRAIN_DATA_SOURCE_* env_vars 读 dataset 配置
ds = auto_dataset(
    transform_fn=VoxelizeActor,
    transform_concurrency=(4, 16),
    batch_size=6,
    do_materialize=True,
)

for batch in ds:
    train_step(batch)
```

平台后端在提交 Job 时把 `dataset_id` 翻译成 `RAYTRAIN_DATA_SOURCE_*` env_vars 注入。

### 6.4 ray-spill 处理

- 长寿 RayCluster 模板里所有 worker pod 必须挂 `/mnt/ray-spill`（hostPath）
- 配置 ray-shared-h20 / a100 时强制要求 GPU 节点有 `/data3/ray-spill` 目录
- 超出 Plasma 自动 spill，性能上对训练无感

### 6.5 launcher 支持

| launcher | 用途 |
|---|---|
| `native_ddp` | 项目自带多机多卡参数（如 Pointcept） |
| `torchrun` | 标准 PyTorch DDP |
| `accelerate` | HuggingFace Accelerate |
| `ray_train` | Ray Train / TorchTrainer 入口 |
| `custom` | 任意命令兜底 |
| **`ray_data_lance`** | **新加：标准化 Lance + Ray Data + DDP shard 范式**，最小化用户代码 |

`ray_data_lance` 设计：
- 用户只写 `train_step(batch)` 函数和 model
- launcher 自动处理：read_lance → transform → DDP shard → iter_torch_batches → train loop
- 适合"标准训练"快速接入，不适合自定义训练循环

---

## 7. 总体架构

### 7.1 终态架构图

```
┌────────────────────────────────────────────────────────────┐
│   用户浏览器                                                  │
│   - https://raytrain.example.com（Web UI）                  │
│   - https://ws-xxx.raytrain.example.com（Workspace IDE）   │
└──────────────────┬──────────────────────────────────────────┘
                   │ HTTPS
                   ▼
┌──────────────────────────────────────────────────────────────┐
│   Ingress + cert-manager                                      │
│   *.raytrain.example.com → 子域名 / 子路径路由                  │
└──────────────────┬──────────────────────────────────────────┘
                   │
   ┌───────────────┼───────────────────┬─────────────────┐
   │               │                   │                 │
   ▼               ▼                   ▼                 ▼
┌───────┐  ┌──────────────────┐  ┌────────────────┐  ┌──────────────────┐
│ Web   │  │ Workspace Pod     │  │ DevSession Pod │  │ Job RayJob       │
│ UI    │  │ - Jupyter         │  │ - 同 4 IDE      │  │ (Ray Submission  │
│ React │  │ - code-server     │  │ - 训练镜像      │  │  to long-lived   │
└───┬───┘  │ - Projector       │  │ + 同 PVC       │  │  RayCluster)    │
    │      │ - sshd            │  │ + GPU          │  └──────────────────┘
    │      │ - raytrain CLI    │  └───────┬────────┘
    │      │ + 用户 PVC（RWX）  │          │
    │      │ + MinIO 凭据      │          │
    │      └────────┬──────────┘          │
    │               │ raytrain CLI         │
    │               │  调 Control Plane    │
    │               ▼                      ▼
    │      ┌───────────────────────────────────────┐
    │      │ raytrain Control Plane                │
    └────► │  FastAPI                              │
           │  - Auth (JWT, 后续 OIDC)              │
           │  - Workspace / DevSession / Job 管理  │
           │  - Dataset 注册表                     │
           │  - 多租户 / 配额 / 审计               │
           │  - Postgres                          │
           └────┬────────────────────────────────┘
                │
        ┌───────┴────────────────────┐
        │                            │
        ▼                            ▼
   ┌─────────────┐            ┌────────────────────┐
   │ K8s API     │            │ Ray Job API        │
   │ (Pod 起灭)   │            │ (任务投递)          │
   └─────────────┘            └────────┬───────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ 长寿 RayCluster  │
                              │ ray-shared-h20   │
                              │ ray-shared-a100  │
                              │ autoscale 0..N   │
                              │ 镜像含 ray[data] │
                              │ + pylance        │
                              │ + raytrain.data  │
                              └────────┬─────────┘
                                       │
                                       ▼
                              ┌─────────────────────┐
                              │ /mnt/ray-spill (HP) │
                              │ Plasma 溢出区       │
                              └─────────────────────┘
                                       │
                                       ▼
                       ┌──────────────────────────────┐
                       │ MinIO                         │
                       │ - 公共: pointcept-data/...   │
                       │         occ-lance/...        │
                       │ - 个人: u-zhangsan-exp/...   │
                       │         u-zhangsan-data/...  │
                       │ - raytrain-code（7d lifecycle）│
                       └──────────────────────────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ MLflow + GitLab  │
                              └──────────────────┘
```

### 7.2 控制面唯一持有 K8s + Ray 凭据

- 用户的浏览器 / Workspace / DevSession **不直接调 K8s 或 Ray API**
- 所有操作经 Control Plane，由它代调
- Control Plane 用一个集群级 ServiceAccount，权限按需细化

### 7.3 关键路径：用户从 Workspace 提交一次 Lance 训练

```
1. 用户在 VS Code 终端打开 raytrain CLI（已预装在 Workspace 镜像）
   raytrain submit --config configs/x.py --dataset nuscenes-v1-train --gpus 8

2. CLI 读 ~/.raytrain/config.yaml（pod 启动时注入的 server URL + token）
   → 调 Control Plane

3. Control Plane:
   ├─ 验 token → user
   ├─ 校验 Job 配额（GPU、并发）
   ├─ 查 Dataset："nuscenes-v1-train" → 取 uri / version / 用户是否有权读
   ├─ 从 Workspace PVC 读用户当前代码 → 打包 zip → 上传 raytrain-code/zhangsan/<job>.zip
   ├─ 写 jobs 表
   ├─ 调 Ray Job Submission API（target=ray-shared-h20）：
   │     entrypoint = "python -m raytrain.entrypoint.driver --from-env"
   │     runtime_env = {
   │       "working_dir": "s3://raytrain-code/zhangsan/...zip",
   │       "env_vars": {
   │         "RAYTRAIN_DATA_SOURCE_TYPE": "lance",
   │         "RAYTRAIN_DATA_SOURCE_URI": "s3://occ-lance/nuscenes_v1",
   │         "RAYTRAIN_DATA_SOURCE_VERSION": "latest",
   │         "RAYTRAIN_USER": "zhangsan",
   │         "RAYTRAIN_RUN_ID": mlflow_run_id,
   │         "AWS_ENDPOINT_URL": "http://minio.../",
   │         ... AWS 凭据等
   │       },
   │       "config": {"setup_timeout_seconds": 600}
   │     }
   └─ 返回 submission_id

4. Ray 长寿 cluster:
   ├─ 拉 working_dir zip（MinIO） → 解压 → chdir
   ├─ pip install 项目额外依赖（runtime_env.pip，如有）
   ├─ 跑 raytrain.entrypoint.driver
   │     - 从 env vars 反序列化 manifest/plan
   │     - 创建 placement group
   │     - 启动 NodeLauncher actors
   ├─ 训练子进程内
   │     - from raytrain.data import auto_dataset
   │     - ds = auto_dataset(transform_fn=...)  ← 读 RAYTRAIN_DATA_SOURCE_*
   │     - ray.data.read_lance(uri) → ActorPool → DDP shard → iter_torch_batches
   │     - train loop
   └─ checkpoint → MinIO，metrics → MLflow

5. UI 任务列表实时刷新（SSE）
```

---

## 8. 关键设计决策

### 8.1 数据集 URL 不绑 bucket

**决策**：Dataset 注册表只存 URI，**不限定 MinIO bucket**。

理由：
- 你们已有 `pointcept-data` / `occ-lance` / 个人 bucket，约定不该被平台改写
- 用户 register 时填自己的 URI，权限走 MinIO bucket policy

可见性 3 级：
- `private`：只有 owner 自己可见
- `tenant`：同租户成员可见
- `public`：全平台可见（admin 审核）

### 8.2 个人 bucket 怎么处理

**决策**：保留现有约定 `u-<user>-exp` / `u-<user>-scratch` / 用户自己创建的 bucket。

平台**不主动创建**用户 bucket（避免和现有运维冲突），仅：
- Workspace 启动时自动注入用户的 MinIO 凭据
- Dataset 注册时校验"URI 是否可读"（HEAD object）
- 可选：Workspace 详情页提供"我的 bucket"快捷入口

未来增强：admin 可以选择"用户开通时自动建 bucket"，但 v1 不做。

### 8.3 long-lived RayCluster 的镜像必须含 Lance 全家桶

**决策**：长寿 cluster 镜像必装：
- `ray[default]==2.54.1`
- `pylance>=0.20`
- `pyarrow>=15.0`
- `daft>=0.4`（可选，给某些场景用）
- `raytrain`（CLI + raytrain.data）
- 项目无关，只装环境

镜像维护：从现有 `172.31.9.104:5050/training/pointcept:ray2.54.1-...` 系列**派生瘦身版**：
- 去掉 `COPY pointcept/`、`COPY tools/` 等代码层
- 加上 raytrain.data 子包

### 8.4 Workspace 镜像分两层

| 镜像 | 内容 | 用途 |
|---|---|---|
| `raytrain-workspace:cpu-base` | 4 IDE + Python + ray[data] + pylance + raytrain | 写代码（CPU） |
| `raytrain-workspace:gpu-pointcept` | cpu-base + CUDA + 项目依赖（如 pointcept） | DevSession 用 |

按 admin 维护的镜像白名单展示给用户选。

### 8.5 ray-spill 是平台标配

**决策**：长寿 cluster 模板强制 `/mnt/ray-spill` hostPath。

理由：
- Plasma 内存有限（一般 30% 节点内存）
- Lance 数据 + ActorPool transform 输出会很大
- 不 spill 会出现内存压力 / OOM
- 现有 raytrain 模板已经这么做，沿用

部署要求：所有 GPU 节点需有 `/data3/ray-spill` 目录（admin 准备 Ansible / 节点初始化脚本）。

### 8.6 ray_data_lance launcher 设计

新增 launcher type `ray_data_lance`，**封装"零拷贝标准范式"**：

```yaml
# .raytrain.yaml
launcher:
  type: ray_data_lance
  entrypoint: my_train.py
  args:
    - --epochs=50
    - --batch-size=6
  env:
    PYTHONPATH: /workspace/my-project
```

用户的 `my_train.py` 只需：

```python
def train_step(batch, model, optim):
    """每个 batch 调一次。用户只写这个。"""
    ...

def build_model():
    return ...

# 入口由 raytrain 负责
```

raytrain driver 自动：
1. 读 `RAYTRAIN_DATA_SOURCE_URI` 跑 `auto_dataset`
2. 跑 DDP init
3. shard dataset
4. 循环 train_step
5. 写 checkpoint / MLflow

这样降低新项目接入门槛。**老项目继续用 native_ddp / ray_train**。

### 8.7 GitLab 集成

Workspace 内置：
- `git` 工具
- `~/.gitconfig`（用户 UI 上配 name/email）
- `~/.ssh/`（用户 UI 上传公钥到 GitLab）

UI 提供"克隆仓库"按钮（输入 git URL → Workspace 内执行 git clone）。

短期不做 OAuth 集成（让用户用自己的 SSH key 到 GitLab）。

### 8.8 RWX 存储用 Longhorn RWX

**决策**：Longhorn RWX 已在生产用了一段时间，**直接复用**。不引入 JuiceFS / CephFS。

理由：
- 现有集群已有 share-manager pod 工作正常
- 只需创建 PVC 时声明 `accessModes: [ReadWriteMany]`
- 性能在我们用例下足够（代码、checkpoint，不读巨大数据）

### 8.9 多 IDE 全装

跟 v2 一致：A + B + C + D 全装到镜像。supervisord 管理 4 个服务，Ingress 子路径暴露。

---

## 9. 路线图

### 9.1 7 个 Milestone

| 编号 | 名称 | 工期 | 对外可见的能力 |
|---|---|---|---|
| **M0** | 代码免镜像 | 1.5 周 | 改完代码不用 build 镜像（CLI 路径，立即受益） |
| **M1** | 控制面 v0 + 长寿 cluster + Lance 通路验证 | 3 周 | API 雏形 + Job 提交链路打通 + 长寿 cluster Lance 通路 |
| **M2** | Workspace v1 | 4 周 | 浏览器申请开发机，4 IDE，Lance 调试可用 |
| **M3** | DevSession + Dataset 注册表 | 2 周 | GPU 调试 + 数据集 CRUD（含 Lance 自动扫元数据） |
| **M4** | 多租户 + 配额 + 审计 + 标准 transform 库 + ray_data_lance launcher | 2 周 | 管理员 CLI + raytrain.data.transforms 发布 |
| **M5** | Web UI v1 | 4 周 | 全功能浏览器界面 |
| **M6** | 上线打磨 | 2 周 | 文档、监控、灰度、GA |

合计 **18.5 周（约 4.5 个月）**

### 9.2 三个对外里程碑

```
β1 (M0+M1)：CLI 路径已平台化，Lance 通路通过长寿 cluster 验证
β2 (M0–M5): 完整 Web UI + 数据集注册表
GA (M0–M6): 全员可用，Kasm + 老 raytrain 退役
```

### 9.3 时间线

```
                Week:  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15 16 17 18 19
M0 代码免镜像             ██░
M1 控制面 v0                  ████████░
M2 Workspace                          ████████████░
M3 DevSession+Dataset                              ██████░
M4 租户/配额/transforms                                    ██████░
M5 Web UI                                                         ████████████░
M6 上线打磨                                                                    ██████

里程碑                       β1                       β2                          GA
```

---

## 10. Milestone 详细任务

> 只列差异；与 v2 一致的部分不重复。

### M0 · 代码免镜像（1.5 周）

| # | 任务 | 验收 |
|---|---|---|
| M0-1 | `raytrain/code_sync.py` | 单测通过 |
| M0-2 | 改 `templates/rayjob.yaml.j2` 加 `runtimeEnvYAML.working_dir` | 渲染测试通过 |
| M0-3 | 改 `entrypoint/driver.py` 加 `_resolve_workdir` | 单测覆盖 3 路径 |
| M0-4 | 改 `cli/submit.py` 5 阶段输出 | dry-run 对齐 |
| M0-5 | 部署 `raytrain-code` MinIO bucket + 7d lifecycle | `mc ilm export` 验证 |
| M0-6 | `.raytrainignore` 默认模板 + 文档 | 文档同事 review |
| M0-7 | 灰度 1 用户 + 全量 | 0 严重 bug |

### M1 · 控制面 v0 + 长寿 cluster + Lance 通路（3 周）

| # | 任务 | 验收 |
|---|---|---|
| M1-1 | namespace `raytrain-shared` + `raytrain-system` | 部署完成 |
| M1-2 | 部署 ray-shared-h20（先一份，后续可加 a100） | head Running，autoscaler 启用 |
| M1-3 | 长寿 cluster 镜像构建：base + ray[data] + pylance + raytrain | 镜像 build 成功 |
| M1-4 | 创建 `raytrain-server/` FastAPI 项目骨架 | 跑得起来 |
| M1-5 | 实现 `/healthz` `/readyz` | 探针通过 |
| M1-6 | JWT 验证 + raytrain 自签发 | 单测通过 |
| M1-7 | `PUT /v1/code` 上传 zip | 集成测试通过 |
| M1-8 | `POST /v1/jobs` 调 Ray Submission API | mock 测通过 |
| M1-9 | `GET /v1/jobs/{id}/logs` SSE | 流式日志正确 |
| M1-10 | `DELETE /v1/jobs/{id}` | stop 调用 Ray |
| M1-11 | `GET /v1/jobs` 列表 | 分页正常 |
| M1-12 | 部署 server 到 `raytrain-system` ns | `/healthz` 200 |
| M1-13 | CLI `cluster_mode: shared` 分支 | shared 跑通 |
| M1-14 | driver 支持从 env vars 读 manifest | 单测通过 |
| M1-15 | `issue-token.sh` | 签发 + 验证 |
| **M1-16** | **Lance 通路 smoke test：长寿 cluster + read_lance + 第一个 step 出 loss** | β1 核心验收 |
| M1-17 | β1 灰度 5 人 1 周 | 反馈收集 |

### M2 · Workspace v1（4 周）

| # | 任务 |
|---|---|
| M2-1 | 设计 `raytrain-workspace:cpu-base` Dockerfile |
| M2-2 | 集成 Jupyter Lab |
| M2-3 | 集成 code-server |
| M2-4 | 集成 PyCharm Projector |
| M2-5 | 集成 sshd + 公钥认证 |
| M2-6 | supervisord 管理 4 服务 |
| M2-7 | 预装 ray[data] / pylance / raytrain.data |
| M2-8 | 预装 raytrain CLI + token 注入机制 |
| M2-9 | git + 默认 .gitconfig 模板 |
| M2-10 | `Workspace` 数据模型 + DB schema |
| M2-11 | `POST /v1/workspaces` 创建 |
| M2-12 | `GET /v1/workspaces`、`/{id}` |
| M2-13 | start / stop / delete |
| M2-14 | RWX PVC（Longhorn） + multi-pod 共享挂载验证 |
| M2-15 | Ingress 子域名路由 |
| M2-16 | SSH LoadBalancer / NodePort |
| M2-17 | 30 天无活动自动 stop CronJob |
| M2-18 | MinIO 凭据自动注入（含个人 bucket） |
| M2-19 | "Lance 调试 demo" smoke：Workspace 里 read_lance 成功 |

### M3 · DevSession + Dataset 注册表（2 周）

| # | 任务 |
|---|---|
| M3-1 | `raytrain-workspace:gpu-pointcept` 镜像（含 CUDA + 训练依赖） |
| M3-2 | DevSession 数据模型 + 创建 GPU pod |
| M3-3 | DevSession 共享 Workspace PVC |
| M3-4 | DevSession 心跳 + 4h/24h 自动回收 |
| M3-5 | DevSession 4 IDE 入口 |
| **M3-6** | **`datasets` 表 + DB schema（含 visibility）** |
| **M3-7** | **`POST/GET/PATCH/DELETE /v1/datasets`** |
| **M3-8** | **Lance 自动扫元数据：schema / rows / size / versions** |
| **M3-9** | **Dataset 权限校验（owner/tenant/public + MinIO HEAD probe）** |

### M4 · 多租户 + 配额 + 审计 + 标准库（2 周）

| # | 任务 |
|---|---|
| M4-1 | 部署 Postgres |
| M4-2 | 用户/租户/Token CRUD |
| M4-3 | 配额引擎（GPU + Workspace + Job） |
| M4-4 | RBAC 中间件 |
| M4-5 | 审计中间件 |
| M4-6 | Admin CLI |
| **M4-7** | **`raytrain.data.transforms.pointcloud` 模块（VoxelizeActor / RandomFlip 等）** |
| **M4-8** | **`raytrain.data.transforms.image`（图像通用 transforms）** |
| **M4-9** | **`ray_data_lance` launcher 实现** |
| M4-10 | Token rotate / revoke |

### M5 · Web UI v1（4 周）

按 v2 拆，新增页面：
- 数据集页（列表 / 详情 / 注册表单 / Schema 浏览 / 谁在用）
- 提交训练表单加"选 Dataset"下拉

### M6 · 上线打磨（2 周）

按 v2，新增：
- Lance 数据通路监控仪表盘（Plasma 命中率、read throughput、ActorPool 利用率）
- 用户文档增加"如何注册 Lance 数据集"、"如何用 ray_data_lance launcher"

---

## 11. 数据模型与 API 契约

### 11.1 Postgres Schema 新增 `datasets` 表

```sql
CREATE TABLE datasets (
    id            UUID PRIMARY KEY,
    name          TEXT NOT NULL,
    type          TEXT CHECK (type IN ('lance', 'parquet', 'dir')),
    uri           TEXT NOT NULL,
    version       TEXT DEFAULT 'latest',
    visibility    TEXT CHECK (visibility IN ('private', 'tenant', 'public')),
    owner_id      UUID REFERENCES users(id),
    tenant_id     UUID REFERENCES tenants(id),
    schema_json   JSONB,
    rows          BIGINT,
    size_bytes    BIGINT,
    tags          TEXT[],
    description   TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(owner_id, name)            -- 同一用户下唯一
);
CREATE INDEX idx_datasets_visibility ON datasets(visibility);
CREATE INDEX idx_datasets_owner ON datasets(owner_id);
CREATE INDEX idx_datasets_tenant ON datasets(tenant_id);
```

`jobs` 表新增字段：

```sql
ALTER TABLE jobs ADD COLUMN dataset_id UUID REFERENCES datasets(id);
```

### 11.2 API 契约新增

```
# Dataset
POST   /v1/datasets                       注册（自动扫 metadata）
GET    /v1/datasets                       列表（按可见性过滤）
GET    /v1/datasets/{id}                  详情（含 schema）
PATCH  /v1/datasets/{id}                  改 tags/desc/visibility
DELETE /v1/datasets/{id}                  删除（不删数据，仅删注册）
POST   /v1/datasets/{id}/refresh          重扫 metadata
GET    /v1/datasets/{id}/preview          前 10 行预览
GET    /v1/datasets/{id}/usage            谁在用（活跃 Job 列表）
```

### 11.3 Job 提交支持 `dataset_id`

```
POST /v1/jobs
{
  "workspace_id": "...",
  "dataset_id": "...",     ← 新增（可选）
  "data_source": {         ← 也支持手填（不通过 Dataset 注册表）
    "type": "lance",
    "uri": "..."
  },
  "gpus": 8,
  "gpu_type": "h20",
  ...
}
```

`dataset_id` 和 `data_source` 二选一。

---

## 12. 部署与运维

### 12.1 K8s 拓扑

```
namespace                      内容
─────────────                  ─────────────────────
raytrain-system                Control Plane Deployment（≥2 副本）
                               Postgres StatefulSet（M4 后）
                               Ingress

raytrain-shared                ray-shared-h20 / a100 RayCluster

raytrain-workspaces            用户 Workspace Pod + PVC（动态）

raytrain-dev                   DevSession Pod（动态）

ray-cluster-3 (legacy)         per-job RayJob（兜底，保留 6 个月）
```

### 12.2 镜像清单

| 镜像 | 内容 | 频率 |
|---|---|---|
| `raytrain-server:vN` | Control Plane | 每 release |
| `raytrain-console:vN` | 前端静态 | 每 release |
| `raytrain-workspace:cpu-base` | 4 IDE + Python + ray[data] + pylance + raytrain | 月级 |
| `raytrain-workspace:gpu-pointcept` | cpu-base + CUDA + pointcept 依赖 | 月级 |
| `raytrain-shared-cluster:vN` | 长寿 cluster 用，环境层 + ray[data] + pylance + raytrain | 月级 |

### 12.3 RWX 存储

继续用 Longhorn RWX。

### 12.4 监控

| 类别 | 指标 |
|---|---|
| API | QPS、p99、5xx |
| Workspace / DevSession | 活跃数、PVC 用量、生命周期 |
| Job | 提交速率、排队时长、成功率 |
| **Lance 数据通路** | **Plasma 命中率、read_lance throughput、ActorPool 利用率、ray-spill 用量** |
| RayCluster | worker 数、autoscaler 触发 |
| MinIO | code bucket 用量、个人 bucket 用量 |

---

## 13. 风险评估

| # | 风险 | 等级 | 应对 |
|---|---|---|---|
| R1 | 镜像 4 IDE 装太大 | 中 | 多阶段 build；按需启动 |
| R2 | Longhorn RWX 性能瓶颈 | 中 | 监控；必要时换 JuiceFS（已有 MinIO） |
| R3 | Workspace 长期占资源 | 中 | 30 天无活动自动 stop |
| R4 | DevSession GPU 滥用 | 中 | 4h 回收 + admin 看板 |
| R5 | Control Plane 单点 | 高 | ≥2 副本；DB 主从 |
| R6 | 长寿 cluster 升级影响 | 中 | drain 流程 |
| R7 | Plasma 命中率低 / 频繁 spill | 中 | 监控；调 object-store-memory |
| R8 | Dataset 注册表里 URI 失效 | 低 | 注册时 HEAD probe；定期巡检 |
| R9 | 用户在 public dataset 上修改 | 中 | public dataset 只读，admin 审核 |
| R10 | Postgres 故障 | 高 | PITR + 主从 |
| R11 | Lance 版本兼容（pylance 升级） | 中 | 镜像锁版本，月度升级窗口 |
| R12 | 用户拒绝迁移 | 中 | 灰度 + 效率差距说服 |

---

## 14. 团队与时间

| 配置 | 总工期 |
|---|---|
| 你 1 人 + 前端用模板 | 6–7 个月 |
| 你 1 人 + 0.5 名前端 | 5 个月 |
| 你 1 人 + 1 名全职前端 | **约 4.5 个月** |
| 你 1 人 + 1 名前端 + 1 名 DevOps | 3.5 个月 |

---

## 15. 待确认事项

按这次回答更新后剩余：

### A. 资源与运维
- [ ] **Q1**：个人 bucket（`u-<user>-...`）目前是手工创建的吗？平台开通用户时要不要自动建？
- [ ] **Q2**：长寿 RayCluster 的 worker pod 是否需要 `/data3/ray-spill` 这条 hostPath？现有节点上这个目录都已经准备好了吗？

### B. 工程
- [ ] 平台域名 `raytrain.<公司域名>`？
- [ ] cert-manager 用哪个 ClusterIssuer？
- [ ] 默认每用户 GPU 上限 / Workspace 数 / Job 并发？
- [ ] Postgres：现有 RDS / 自建 / K8s StatefulSet？
- [ ] 前端配置：0.5 / 1 / 模板？
- [ ] 灰度志愿者是哪几位？

### C. 范围
- [ ] Dataset 注册表 v1 是否需要支持 parquet 和 dir？还是只 lance（其他后续）？
- [ ] `ray_data_lance` launcher 是否需要 v1 就上？（M4 才出）
- [ ] Public dataset 审核流程（admin 一键 approve 即可？）

---

## 附录 A：第一周动手清单

**Day 1-2 (M0 主体)**：
- 写 `raytrain/code_sync.py`（打包、SHA256、上传、3 次重试）
- 部署 `raytrain-code` MinIO bucket + 7d lifecycle
- 单元测试

**Day 3-4 (M0 收口)**：
- 改 `templates/rayjob.yaml.j2` 加 `runtimeEnvYAML.working_dir` + `setup_timeout_seconds: 600`
- 改 `entrypoint/driver.py` 加 `_resolve_workdir`（识别 Ray 解压路径）
- 改 `cli/submit.py` 5 阶段进度
- 灰度 1 用户跑 pointcept smoke

**Day 5+ (M1 启动)**：
- 部署一份 ray-shared-h20 长寿 RayCluster（先用现有镜像 + 加 `pylance/ray[data]`）
- 用 `ray job submit` 直接验证 Lance 通路
- 启动 raytrain-server 仓库骨架

## 附录 B：术语表

| 术语 | 定义 |
|---|---|
| Workspace | 长寿 CPU 开发机 |
| DevSession | 短期 GPU 会话（绑 Workspace） |
| Job | 训练任务 |
| Dataset | 平台注册的数据集（含 Lance 元数据） |
| Tenant | 租户（团队） |
| Lance | 列式向量化数据格式（pylance） |
| Ray Data | Ray 自带流式数据处理 |
| Plasma | Ray object store（共享内存） |
| ray-spill | Plasma 溢出到本地盘的目录 |
| RWX PVC | ReadWriteMany 卷 |
| code-server | 浏览器版 VS Code |
| Projector | 浏览器版 PyCharm |

## 附录 C：变更历史

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-05-28 | v1 | 初稿（CLI 平台化） |
| 2026-05-28 | v2 | 浏览器一站式、Workspace/DevSession、4 IDE |
| 2026-05-28 | v3 | 聚焦 Ray Data + Lance；新增 Dataset 注册表 + ray_data_lance launcher；明确个人/公共 bucket 区分 |
