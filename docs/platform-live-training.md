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

1. 新建开发机 = 常驻 CPU Pod + RWX 持久卷，内置 Jupyter / VS Code / SSH。状态从
   `creating`→`running`（由后端从 K8s 真实派生，不再「秒 running」）；**`running` 后**卡片上
   才出现 IDE 链接（NodePort，`http://<node>:<nodePort>/`）与 SSH（`ssh://<node>:<nodePort>`）。
2. 需要 GPU 联调 → 卡片上「挂 GPU」开一个 **DevSession**：挂同一个持久卷（代码共享），1–8 卡，
   空闲超时自动回收（后端 reclaim 循环），不浪费 GPU。
3. 在开发机里调通后，直接 `raytrain submit`（A 路）或回浏览器 Create Job（B 路）提交训练。

前置：集群有 `raytrain-workspaces` 命名空间 + workspace 镜像（`deploy/Dockerfile.workspace-cpu`
等），server 的 ServiceAccount 有该命名空间的 pod/pvc/svc 权限（已在
`raytrain-server/deploy/serviceaccount.yaml` 配好）。

---

## 5. 端到端 0→1 验收清单（有集群时）

按顺序逐条勾，全绿即视为「从 0 到 1 真实跑通」：

- [ ] **持久化**：`RAYTRAIN_DATABASE_URL` 指向 Postgres、`RAYTRAIN_SEED_DEMO=false`；
      重启后端后 job/资源/队列别名仍在（不丢）。
- [ ] **集群可达**：`RAYTRAIN_SHARED_CLUSTERS` 配了 h20，head `:8265/api/version` 可达。
- [ ] **队列真实**：Console → Queues 显示的就是集群 `kubectl get localqueue` 的队列；
      读不到时页面显式报错（不回退假数据）。
- [ ] **登录**：用引导管理员账号密码登录；顶栏 GPU/CPU/MEM 配额来自 `/v1/quota`。
- [ ] **开发机**：新建开发机 → 状态从 `creating`→`running`（不再「秒 running」）；
      `running` 后 IDE/SSH 链接可点（NodePort），进 Jupyter/VS Code 能编辑代码。
- [ ] **停后再启**：停止开发机 → 状态 `stopping`→`stopped`；再启动能成功（Terminating
      未清干净时返回友好 409 提示，等待后可重试）。
- [ ] **提交训练**：开发机终端 `raytrain submit ...`（A 路）或浏览器 Create Job 传
      `code_uri`（B 路）；队列候选来自真实 Kueue，无队列时阻止提交。
- [ ] **LIVE**：Job Detail 顶部出现 **● 集群运行中**，submissionId 非空。
- [ ] **真实 Pods/Events**：Job Detail → Pods 显示真实 head/worker；Events 显示翻译后的
      K8s 事件；非 live 任务显式标注「未真实提交」。
- [ ] **日志接 Loki**：Logs 页显示 `source: 来自 Loki` 的真实训练日志（loss 在降），
      **任务结束后仍可查**；未配 Loki 时显示「不可用」而非伪造。
- [ ] **指标接 Prometheus**：Metrics 页显示 `source: 来自 Prometheus` 的 GPU 利用率/显存/
      吞吐；无数据/未配置显示「不可用」。
- [ ] **Artifacts 真实**：训练写出 checkpoint 后，Artifacts 页/Job Detail 从 MinIO 列出
      真实文件（按名分类 checkpoint/model/log/eval）；非 s3:// 路径显示「不可用」。
- [ ] **Lance**：选了 Lance 数据集时，日志里有 `ray.data.read_lance` 的读取/分片信息。
- [ ] `kubectl -n raytrain-shared get pods` 能看到该 RayJob 拉起的 worker。
- [ ] **取消**：Cancel 后 Ray job 进入 STOPPED，Console 状态变 Cancelled。
- [ ] **i18n**：顶栏切到 EN，全站英文；刷新后保持（localStorage）。

## 6. 没有集群时（本机 / 单节点）

默认行为已收紧为「**不伪造**」：提交一个 gpu_type 没有配置集群的任务会被后端
**拒绝**（`NO_CLUSTER` 友好错误），而不是生成一条永远 Queued 的假记录。

如果你确实想在**无集群**环境演示 UI 全流程，显式打开 record-only 开关：

```yaml
RAYTRAIN_ALLOW_RECORD_ONLY_SUBMIT: "true"   # 仅演示用；生产保持 false
```

此时提交会落库为平台记录（状态 Queued，无 LIVE 标记），Pods/Events/Logs/Metrics 均显式
标注「不可用」（不合成）。这就是 `deploy/local-singlenode` 的演示模式——验证平台本身，
不跑真实 GPU 训练。

## 7. 排障

| 现象 | 排查 |
| --- | --- |
| 提交后不是 LIVE | 该 gpu_type 不在 `RAYTRAIN_SHARED_CLUSTERS`；或 head 不可达 |
| Job 立刻 Failed (SubmitError) | 看 server 日志 `submission.ray_failed`；多为 head URL 错或集群没起 |
| worker 起不来 | `kubectl -n raytrain-shared describe pod`；常见 GPU 不足 / 镜像拉取失败 |
| Lance 读不到 | `RAYTRAIN_DATA_SOURCE_URI` 是否注入；MinIO endpoint/key 是否在 env；URI 是否到 `.lance` 根 |
| 代码改了没生效 | 确认走了 code_uri（working_dir），不是镜像内旧代码；重新打包提交 |
```
