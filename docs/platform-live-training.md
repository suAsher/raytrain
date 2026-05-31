# 让训练真正跑到集群（Ray + Ray Data/Lance + code-as-submission）

本文承接 `platform-deploy.md`，讲**怎么把"提交"从平台记录变成真实在 GPU 集群上运行的
RayJob**，并用上 Ray Data / Lance 流式读数。读完照做即可端到端跑通。

> 现状（已实现，且有单测覆盖）：
> - 浏览器 Create Job → 后端 `SubmissionService` 在 gpu_type 配了集群时**真实提交 RayJob**；
>   否则退化为平台记录（dev 模式），UI 照常工作。
> - 代码即提交：`working_dir` 指向 MinIO 里的 code zip，Ray 在每个 worker 拉取解压。
> - Ray Data/Lance：选中的数据集 URI 注入 `RAYTRAIN_DATA_SOURCE_URI`，训练代码用
>   `raytrain.data.auto_dataset()` → `ray.data.read_lance()` 从 MinIO 流式读。
> - 列表/详情会从 Ray **回填真实状态**；详情页「实时日志」拉 Ray 日志。

---

## 1. 让"提交"变真实：配置共享集群

后端按 `gpu_type` 找集群地址：`settings.shared_clusters`（环境变量
`RAYTRAIN_SHARED_CLUSTERS`，JSON）。**只要某个 gpu_type 在这里有地址，对应提交就真实落到
那个 RayCluster 的 Job Submission API。**

```yaml
# raytrain-server/deploy/configmap.yaml
RAYTRAIN_SHARED_CLUSTERS: |
  {
    "h20": "http://ray-shared-h20-head.raytrain-shared.svc.cluster.local:8265",
    "a100": "http://ray-shared-a100-head.raytrain-shared.svc.cluster.local:8265"
  }
```

前置：集群已装 **KubeRay operator**，且按 `raytrain-server/deploy/raycluster-shared-h20.yaml`
起了长寿 RayCluster（head 的 dashboard 端口 8265 通过 Service 暴露）。

验证后端能看见集群：

```bash
kubectl -n raytrain-system exec deploy/raytrain-server -- \
  python -c "import os,json;print(json.loads(os.environ['RAYTRAIN_SHARED_CLUSTERS']))"
# head 可达性：
kubectl -n raytrain-system exec deploy/raytrain-server -- \
  curl -fsS http://ray-shared-h20-head.raytrain-shared.svc.cluster.local:8265/api/version
```

---

## 2. code-as-submission：代码怎么上去的

两条路，都不构建镜像：

**A. 从开发机提交（推荐）** —— 开发机 Pod 里内置 raytrain CLI + MinIO 凭据。用户在 IDE 改完
代码，终端跑 `raytrain submit ...`：CLI 打包当前目录 → `PUT /v1/code`（server 存进 MinIO，
7 天生命周期）→ 拿到 `s3://raytrain-code/<user>/<job>.zip` → 提交时作为 `code_uri`。

**B. 从浏览器 Create Job 提交** —— 向导里可带一个已上传的 `code_uri`（留空则用镜像内代码）。
后端把它设进 `runtime_env.working_dir`，Ray 在每个 worker 拉取解压后执行 entrypoint。

关键点：**改一行代码 → 重新打包提交 → 立刻生效，不 rebuild 镜像**。回退（用镜像内代码）只需
不传 `code_uri`。

---

## 3. Ray Data / Lance：真实流式读数

训练代码这样写（已在 `raytrain/data/` 提供）：

```python
from raytrain.data import auto_dataset
from torch.utils.data import DataLoader

ds = auto_dataset(batch_size=6, prefetch_batches=4, materialize=False)
loader = DataLoader(ds, batch_size=None, num_workers=0)
for epoch in range(epochs):
    for batch in loader:           # 已是 torch tensor（零拷贝 Arrow→Tensor）
        loss = model(batch["coord"], batch["segment"]); loss.backward()
```

平台侧要做的只是**把数据集 URI 注入环境**：在 Create Job 的「Dataset (Lance)」选一个已注册的
Lance 数据集，后端 `SubmissionService` 会注入 `RAYTRAIN_DATA_SOURCE_URI`（连同 MinIO 的
`AWS_ENDPOINT_URL` / key）。`RayLanceDataset` 用这些 env + `ray.data.read_lance()` 直接从
MinIO 流式读，支持列裁剪 / filter / 多 epoch Plasma 缓存 / DDP 分片（见
`raytrain/data/ray_lance_dataset.py` 与 `docs/sslod26-raydata-raytrain-before-after.md`）。

注册数据集：Console → Datasets → Register（或 `POST /v1/datasets`），URI 形如
`s3://lance-datasets/scannet.lance`。注册时会尽力扫描 Lance 元数据（行数 / schema / version）。

---

## 4. 开发机（Workspaces / DevSessions）

Console → **开发机**：

1. 新建开发机 = 常驻 CPU Pod + RWX 持久卷，内置 Jupyter / VS Code / SSH（点卡片上的 IDE 链接
   直接进，SSH 用 `ssh://ws-<id>...:22`）。
2. 需要 GPU 联调 → 卡片上「挂 GPU」开一个 **DevSession**：挂同一个持久卷（代码共享），1–8 卡，
   空闲超时自动回收（后端 reclaim 循环），不浪费 GPU。
3. 在开发机里调通后，直接 `raytrain submit`（A 路）或回浏览器 Create Job（B 路）提交训练。

前置：集群有 `raytrain-workspaces` 命名空间 + workspace 镜像（`deploy/Dockerfile.workspace-cpu`
等），server 的 ServiceAccount 有该命名空间的 pod/pvc/svc 权限（已在
`raytrain-server/deploy/serviceaccount.yaml` 配好）。

---

## 5. 端到端验收清单（有集群时）

- [ ] `RAYTRAIN_SHARED_CLUSTERS` 配了 h20，且 head `:8265/api/version` 可达
- [ ] Console 新建开发机 → 进 Jupyter/VS Code 能编辑代码
- [ ] 开发机终端 `raytrain submit --config ... --gpus 8 --nodes 1 --name smoke`（A 路）
      或浏览器 Create Job 传 `code_uri`（B 路）
- [ ] Job Detail 顶部出现 **● LIVE on cluster**，submissionId 非空
- [ ] 「实时日志」能看到 Ray worker 的真实训练日志（loss 在降）
- [ ] 选了 Lance 数据集时，日志里有 `ray.data.read_lance` 的读取/分片信息
- [ ] `kubectl -n raytrain-shared get pods` 能看到该 RayJob 拉起的 worker
- [ ] Cancel 后 Ray job 进入 STOPPED，Console 状态变 Cancelled

## 6. 没有集群时（本机 / 单节点）

不配 `RAYTRAIN_SHARED_CLUSTERS`（或该 gpu_type 不在表里）时：提交仍成功，但作为**平台记录**
（状态 Queued，无 LIVE 标记），用于演示 UI 全流程。这就是 `deploy/local-singlenode` 的默认
行为——验证平台本身，不跑真实 GPU 训练。

## 7. 排障

| 现象 | 排查 |
| --- | --- |
| 提交后不是 LIVE | 该 gpu_type 不在 `RAYTRAIN_SHARED_CLUSTERS`；或 head 不可达 |
| Job 立刻 Failed (SubmitError) | 看 server 日志 `submission.ray_failed`；多为 head URL 错或集群没起 |
| worker 起不来 | `kubectl -n raytrain-shared describe pod`；常见 GPU 不足 / 镜像拉取失败 |
| Lance 读不到 | `RAYTRAIN_DATA_SOURCE_URI` 是否注入；MinIO endpoint/key 是否在 env；URI 是否到 `.lance` 根 |
| 代码改了没生效 | 确认走了 code_uri（working_dir），不是镜像内旧代码；重新打包提交 |
```
