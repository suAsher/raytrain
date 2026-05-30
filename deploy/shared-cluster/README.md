# 长寿共享 RayCluster 部署运维

面向对象：平台运维。本目录下是 **Phase 2 · Shared_Cluster_Mode** 的核心资源——按 GPU
类型分组、长期在线的共享 RayCluster。本文档讲它的部署、升级（含 Ray 版本升级的 drain
步骤）、排障与下线流程。

相关文件：

| 文件 | 作用 |
| --- | --- |
| `raycluster-h20.yaml` | H20 GPU 池：`RayCluster ray-shared-h20` + `Service ray-shared-h20-head` |
| `raycluster-a100.yaml` | A100 GPU 池：`RayCluster ray-shared-a100` + `Service ray-shared-a100-head` |
| `../Dockerfile.shared-cluster-env` | env-only 镜像（只含运行环境，不含训练代码） |
| `../node-labels.sh` | 给 GPU 节点打 `gpu=h20`/`gpu=a100` 标签 |

通用运维（节点标签、镜像体系、RBAC、NCCL 排障等）见 `docs/ops-guide.md`，本文档只覆盖
共享集群特有的部分，并在需要时交叉引用。

## 概述

这两份清单各自声明一个**长期在线**的 RayCluster，按 GPU 类型分池：

- `ray-shared-h20` —— 调度到打了 `gpu=h20` 标签的节点。
- `ray-shared-a100` —— 调度到打了 `gpu=a100` 标签的节点。

两者都在 namespace `ray-shared` 下，带 label `raytrain.shared: "true"` 和
`raytrain.gpu_type: h20|a100`，用于和 Phase 1 的 per-job RayCluster 区分。

**和 Phase 1 的区别（提交模型）**：Phase 1 里每个任务都现场创建一个 per-job
RayCluster，跑完即销毁。Phase 2 的共享集群模式下，集群常驻不销毁，用户提交时**不再**
创建 RayCluster，而是通过 **Ray Job Submission API**（head 的 8265 端口）把任务投递到
对应 GPU 类型的长寿池里。raytrain `User_Config` 的 `shared_clusters` 指向 head Service：

```yaml
shared_clusters:
  h20:  http://ray-shared-h20-head.ray-shared.svc:8265
  a100: http://ray-shared-a100-head.ray-shared.svc:8265
```

**autoscaling（0 → 16）**：集群开启 KubeRay in-tree autoscaler
（`enableInTreeAutoscaling: true`）。worker 组初始 `replicas: 0`、`minReplicas: 0`，
**空闲时缩到 0 不占 GPU**；有任务投递时按需扩，最多到 `maxReplicas: 16`（即最多 16 块
该类型 GPU 同时在线）。每个 worker pod 申请 1 块 GPU。

> head 是 CPU-only（`num-gpus: "0"`），只跑 GCS / dashboard / Job Submission API，
> 不参与训练。

## 前置条件

部署前逐项确认：

1. **namespace `ray-shared` 存在**：

   ```bash
   kubectl create namespace ray-shared || true
   ```

2. **GPU 节点已打标签**（`gpu=h20` / `gpu=a100`，worker 的 nodeSelector 依赖它）：

   ```bash
   bash deploy/node-labels.sh        # 可重复执行
   kubectl get nodes -L gpu          # 确认目标节点有正确的 gpu 标签
   ```

   节点标签约定见 `docs/ops-guide.md` §1。

3. **hostPath 目录存在于每个 GPU 节点上**（是目录即可，不需要 PV/PVC）：

   | Pod 内路径 | 宿主机路径 | 用途 |
   | --- | --- | --- |
   | `/mnt/ray-cache` | `/data4/ray-cache` | 数据集缓存 + 实验输出 |
   | `/mnt/ray-spill` | `/data3/ray-spill` | Ray object-store spill |

   模板用的是 `hostPath` + `type: DirectoryOrCreate`，节点上缺失会自动建目录，但要确保
   `/data4`、`/data3` 这两个父挂载点在节点上真实存在且可写。

4. **env-only 镜像已构建并推送到内网 registry**：

   YAML 里 head/worker 的 `image` 字段目前是**占位**（沿用项目镜像
   `172.31.9.104:5050/training/pointcept:ray2.54.1-torch2.5.0-cu124`），
   清单注释里标了 `TODO(ops)`。正式部署前必须替换成专门的 env-only 镜像，例如：

   ```
   172.31.9.104:5050/raytrain/raytrain-base-env:ray2.54.1-cu124-v1
   ```

   构建方法见 `deploy/Dockerfile.shared-cluster-env` 顶部注释。env-only 镜像**只含运行
   环境**（Ray + PyTorch + CUDA + Lance + raytrain），不含训练代码——训练代码在提交时
   由 `runtime_env.working_dir` 从 MinIO 拉取。registry 约定见 `docs/ops-guide.md` §4。

5. **KubeRay operator 已安装**（提供 `RayCluster` CRD 和 autoscaler）：

   ```bash
   kubectl get crd rayclusters.ray.io
   kubectl get pods -A | grep kuberay
   ```

## 部署流程

1. 替换 YAML 里两处 `image`（head + worker）为真正的 env-only 镜像 tag（见前置条件 4）。

2. apply 清单（按需部署一个或两个池）：

   ```bash
   kubectl apply -f deploy/shared-cluster/raycluster-h20.yaml
   kubectl apply -f deploy/shared-cluster/raycluster-a100.yaml
   ```

   apply 是幂等的：重复执行只更新 RayCluster / Service，不会删除已有的 RayJob。

3. 验证 RayCluster 和 head pod：

   ```bash
   # 两个共享集群都应列出来
   kubectl -n ray-shared get raycluster -l raytrain.shared=true

   # head pod 应进入 Running（worker 此时为 0，正常）
   kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
   kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-a100
   ```

   预期：每个集群有 1 个 head pod 处于 `Running`；worker 数为 0（还没任务投递）。

4. 确认 head Service / dashboard 可达：

   ```bash
   kubectl -n ray-shared get svc ray-shared-h20-head ray-shared-a100-head

   # 临时端口转发后本地访问 dashboard / Job Submission API
   kubectl -n ray-shared port-forward svc/ray-shared-h20-head 8265:8265
   # 另开一个终端：
   curl -s http://localhost:8265/api/version
   ```

   Service 暴露三个端口：`8265`（dashboard / Job Submission API）、`10001`（Ray client）、
   `6379`（GCS）。

5. （可选）冒烟一个小任务，确认 worker 能从 0 扩起来：投递后用
   `kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20 -w` 观察 worker pod
   被 autoscaler 拉起并进入 `Running`。

## 升级流程

共享集群是声明式的——改 YAML 后 `kubectl apply` 即可。一般的小改动（如调整
`maxReplicas`、资源 request、env）可以直接 apply，KubeRay 会做滚动更新。

**但 Ray / KubeRay 版本升级是高风险操作，必须先 drain。** 见下一节。

### Ray 版本升级 · drain 步骤（重要）

> ⚠️ **为什么要 drain**：head 和 worker 的 Ray 版本必须一致。升级时 autoscaler 会用
> **新版本**镜像拉起 worker，而正在跑的 in-flight 任务的 worker 可能还是**旧版本**；
> head/worker Ray 版本不一致会直接打断这些任务（GCS / 通信协议不兼容）。所以升级前
> 必须先把在跑的任务排空（drain），再滚动更新。

这一点与 `.kiro/specs/long-term-evolution/design.md` 的结论一致：**升级前停止接受新
submission，等所有 in-flight 任务跑完再 rolling-update**。

按顺序执行：

**(a) 停止接受新 submission。** 让用户不能再往该池投递新任务，二选一：

- 把 raytrain Submission_Server 切到 maintenance（或把 `default_cluster_mode` 切回
  `per_job` / 暂时摘掉该 GPU 类型的 `shared_clusters` 入口），让新任务不再路由到这个池；
- 或者作为兜底，临时收紧 head Service 入口 / 暂停 Submission_Server，使 8265 的 Job
  Submission API 不再接收新任务。

  目标只有一个：从这一刻起，**该池不再有新的 RayJob 进来**。

**(b) 等待 in-flight 任务跑完（排空）。** 列出并观察该集群上还在跑的任务：

```bash
# 看该集群当前的 RayJob / 任务 pod
kubectl -n ray-shared get rayjob
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20

# 通过 Job Submission API 看在跑的 job（先 port-forward 8265）
kubectl -n ray-shared port-forward svc/ray-shared-h20-head 8265:8265
ray job list --address http://localhost:8265
```

持续观察直到没有 `RUNNING` / `PENDING` 的 job，worker 也随 autoscaler 缩回到 0。
**不要**强行删除还在跑的任务（除非已和用户确认可以中断）。

**(c) 更新版本并 apply（滚动更新）。** 任务排空后，编辑对应 YAML：

- `spec.rayVersion`（例如 `"2.54.1"` → 新版本）；
- head 和 worker 两处 `image`，换成新版本对应的 env-only 镜像 tag。

```bash
kubectl apply -f deploy/shared-cluster/raycluster-h20.yaml
```

KubeRay 会用新配置重建 head。worker 此时是 0，**autoscaler 会在下次有任务时用新版本
镜像重新拉起 worker**，所以无需手动改 worker 数量。

**(d) 验证新 head / worker 健康。**

```bash
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
kubectl -n ray-shared logs <new-head-pod> | tail -30      # 确认 ray 版本 / GCS 起来
# port-forward 后确认 dashboard 可达、版本正确
curl -s http://localhost:8265/api/version
```

必要时投递一个小冒烟任务，确认 worker 能用新镜像从 0 扩起并跑通。

**(e) 恢复接受 submission。** 把 (a) 的开关复位：Submission_Server 退出 maintenance /
把 `default_cluster_mode` 切回 `shared` / 恢复 `shared_clusters` 入口。通知用户可以
正常提交。

> 两个池（h20 / a100）相互独立，可以分别 drain + 升级，降低同时停服的影响面。

## 排障

通用速查（任务 Pending、placement group、NCCL 等）见 `docs/ops-guide.md` §6，下面是
共享集群特有的排查点。

先备好这几条通用命令：

```bash
kubectl -n ray-shared describe raycluster ray-shared-h20
kubectl -n ray-shared get events --sort-by='.lastTimestamp' | tail -20
kubectl -n ray-shared logs <pod> | tail -50
```

### head pod 一直 Pending

```bash
kubectl -n ray-shared describe pod <head-pod> | tail -30
```

常见原因：

- **节点资源不足**：head 要 2 CPU / 8Gi request，确认有节点能放下。
- **镜像拉不动**：内网 registry `172.31.9.104:5050` 证书/权限问题，或镜像 tag 写错
  （还停在占位镜像、或 env-only tag 没 push）。`describe` 里看 `Failed to pull image`。
- **`imagePullPolicy: Always`**：registry 不可达时会卡在拉取。先确认节点能 `docker pull`
  该 tag。

### worker 扩不起来（0 一直不动）

任务投递了但 worker 始终是 0：

```bash
kubectl -n ray-shared describe raycluster ray-shared-h20   # 看 autoscaler 事件
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
kubectl -n ray-shared describe pod <pending-worker> | tail -30
kubectl describe nodes -l gpu=h20 | grep -i gpu            # 看可分配 GPU
```

常见原因：

- **autoscaler 没工作**：确认 `enableInTreeAutoscaling: true`，且 KubeRay operator
  正常（`kubectl get pods -A | grep kuberay`）。
- **节点缺 `gpu` 标签**：worker nodeSelector 是 `gpu: h20|a100`，节点没打标签就无处可调度。
  跑 `bash deploy/node-labels.sh`，`kubectl get nodes -L gpu` 核对。
- **GPU 耗尽**：满足 nodeSelector 的节点 GPU 都被占了，新 worker 只能 Pending。
- **撞到 `maxReplicas`**：池已扩到 16 块 GPU，再多的任务排队等待，属正常限流。

### hostPath 目录缺失

worker pod 报挂载失败 / CrashLoopBackOff：

```bash
kubectl -n ray-shared describe pod <worker-pod> | tail -30
```

确认目标 GPU 节点上 `/data4/ray-cache`、`/data3/ray-spill` 的父挂载点存在且可写
（见前置条件 3、`docs/ops-guide.md` §1）。

### dashboard / Service 不可达

```bash
kubectl -n ray-shared get svc ray-shared-h20-head -o wide
kubectl -n ray-shared get endpoints ray-shared-h20-head
```

常见原因：

- **head 没 Running**：Service 的 selector 是 `ray.io/cluster=ray-shared-h20` +
  `ray.io/node-type=head`，head 没起来 endpoints 就是空的。
- **端口转发指错**：dashboard / Job Submission API 是 `8265`。
- **`shared_clusters` URL 写错**：应为 `http://ray-shared-<type>-head.ray-shared.svc:8265`。

### NCCL 问题

训练第 0 步卡住等 NCCL 相关问题，与 per-job 一致，见 `docs/ops-guide.md` §6「NCCL
第 0 步卡住」。worker 模板默认 `NCCL_IB_DISABLE=1`、`NCCL_SOCKET_IFNAME=^lo,docker0`，
具体任务可在提交时用 `runtime_env` / `launcher.env` 覆盖。

## 清理 / 下线

> ⚠️ 删除 RayCluster 会终止其上所有在跑的任务。**下线前先按上面的 drain 步骤排空
> in-flight 任务**（停止接受新 submission → 等任务跑完），确认没有 `RUNNING` job 后再删。

排空后删除（按需删一个或两个池）：

```bash
kubectl delete -f deploy/shared-cluster/raycluster-h20.yaml
kubectl delete -f deploy/shared-cluster/raycluster-a100.yaml
```

`kubectl delete -f` 会删掉对应的 RayCluster 和 head Service；KubeRay 随后清理 head /
worker pod。删除后确认：

```bash
kubectl -n ray-shared get raycluster -l raytrain.shared=true
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
```

如果整套共享集群体系都要下线，可在确认 namespace 下没有其它需要保留的对象后，删除
namespace：

```bash
kubectl delete namespace ray-shared
```
