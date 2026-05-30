# raytrain 端到端落地手册（最终效果 + 详细步骤）

本文把 `long-term-evolution` spec 的全部 45 个任务串成一条**从零到跑通**的操作链，
覆盖两种最终形态：

- **Phase 1（code-as-submission）**：用户改完代码 `raytrain submit` 直接跑，**不再 build 镜像**。
- **Phase 2（shared 模式）**：用户**不要 kubeconfig**，只用 token + 浏览器/CLI，通过
  Submission_Server 把任务投到长寿 RayCluster。

> 命令里 `172.31.9.104:5050`（registry）、`ray-cluster-3` / `ray-shared` /
> `raytrain-system`（namespace）、`http://172.31.16.3:30950`（MinIO）都是占位值，按你的
> 环境替换。每一步都标注了对应的 spec 任务号与权威文档，细节去对应文档查。

---

## 0. 最终效果（用户视角）

| 形态 | 用户要做什么 | 用户**不需要**什么 |
| --- | --- | --- |
| Phase 1 / per_job | `raytrain configure` 一次 → 改代码 → `raytrain submit` | 不用 build/push 镜像；不用写 RayJob YAML |
| Phase 2 / shared | `raytrain configure`（填 server + token）→ `raytrain submit --cluster-mode shared` | **不用 kubeconfig / kubectl**；不用 build 镜像 |

一句话：**改完代码直接提交就能跑**；长期看**每人一个 token 取代每人一个 kubeconfig**。

---

## 1. 一次性环境准备（运维）

### 1.1 节点与 namespace

```bash
kubectl create namespace ray-cluster-3 || true     # per_job 用户提交 ns
bash deploy/node-labels.sh                          # 给 GPU 节点打 gpu=h20 / gpu=a100
kubectl get nodes -L gpu                            # 核对
```

GPU 节点上需存在 hostPath 目录 `/data4/ray-cache`、`/data3/ray-spill`（见
`docs/ops-guide.md` §1）。

### 1.2 Code Bucket（Phase 1 的核心依赖，任务 3.1）

```bash
MINIO_ENDPOINT=http://172.31.16.3:30950 \
MINIO_ACCESS_KEY=xxx MINIO_SECRET_KEY=xxx \
    bash deploy/setup-code-bucket.sh
# 验证：应出现 Days: 7 的 lifecycle
mc ilm export raytrain-setup/raytrain-code
```

bucket 名、配额、紧急清理见 `docs/ops-guide.md` §9。

### 1.3 镜像：只放环境，不放代码（任务 4.3 / 5.2）

镜像现在只承载环境（CUDA/torch/ray/依赖 + raytrain 本体），训练代码走 working_dir。
构建见 `docs/ops-guide.md` §4 与 `docs/adding-new-repo.md`。改训练代码**不需要**重建镜像。

---

## 2. Phase 1：改完代码直接提交（用户）

对应任务 1.x / 2.x，机制全文见 `docs/user-guide.md` §9。

```bash
# 1) 一次性配置凭据（MinIO / MLflow / namespace）
raytrain configure

# 2) 进项目目录，改你的训练代码 / config（无需动 .raytrain.yaml）
cd ~/pointcept-main

# 3) 先 dry-run 看渲染（5 阶段：打包→上传→MLflow→渲染→apply）
raytrain submit --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 1 --nodes 1 --gpu-type h20 --name smoke --dry-run

# 4) 真正提交
raytrain submit --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 1 --nodes 1 --gpu-type h20 --name smoke

# 5) 看日志 / 状态 / 停止
raytrain logs <job_name> -f
raytrain list
raytrain stop <job_name>
```

成功标志：日志出现 `[driver] code_hash=<前12位>` 与训练 `loss=`，MLflow run 带
`raytrain.code_uri` / `raytrain.code_hash` tag（任务 2.5 / 11.2）。

**回退到镜像内代码**：加 `--no-code-sync`（任务 4.5）。
**精确复现某次 run 的代码**：`raytrain reproduce <mlflow_run_id>`（任务 11.1）。

排除规则（哪些不进 zip）、200 MiB 上限、`.raytrainignore` 用法见 `docs/user-guide.md` §9.2–9.4。

---

## 3. Phase 2：长寿集群 + token（运维部署）

完整迁移手册见 `docs/migration-shared-cluster.md`；下面是最短路径。

### 3.1 部署长寿 RayCluster（任务 6.x）

```bash
kubectl create namespace ray-shared || true
# 先把 raycluster-*.yaml 里 head/worker 的 image 换成 env-only 镜像（注释里有 TODO(ops)）
kubectl apply -f deploy/shared-cluster/raycluster-h20.yaml
kubectl apply -f deploy/shared-cluster/raycluster-a100.yaml
kubectl -n ray-shared get raycluster -l raytrain.shared=true   # head Running、worker=0 正常
```

升级 Ray 版本要先 drain，见 `deploy/shared-cluster/README.md`。

### 3.2 部署 Submission_Server（任务 7.x）

```bash
# 构建并推送 server 镜像（纯 Python，轻量）
docker build -f deploy/server/Dockerfile \
    -t 172.31.9.104:5050/raytrain/raytrain-server:0.1.0 .
docker push 172.31.9.104:5050/raytrain/raytrain-server:0.1.0

# 关键：issue-token 与 server 必须用同一个 JWT 密钥
VAL="$(openssl rand -hex 32)"
kubectl -n raytrain-system create secret generic raytrain-jwt-key \
    --from-literal=RAYTRAIN_JWT_SECRET="$VAL"
kubectl -n raytrain-system create secret generic raytrain-server-secrets \
    --from-literal=RAYTRAIN_JWT_SECRET="$VAL"

# apply（含 Namespace/ConfigMap/Deployment/Service/Ingress）；改好 image tag 与 Ingress host/tls
kubectl apply -f deploy/server/deployment.yaml
kubectl -n raytrain-system rollout status deploy/raytrain-server

# 自测
kubectl -n raytrain-system run curl --rm -it --image=curlimages/curl --restart=Never -- \
    curl -fsS http://raytrain-server.raytrain-system.svc:8080/healthz   # {"status":"ok"}
```

server 读的环境变量：`RAYTRAIN_JWT_SECRET`（鉴权）、`RAYTRAIN_SHARED_CLUSTERS`（JSON：
gpu_type→head URL，由 ConfigMap 注入）、可选 `RAYTRAIN_OIDC_*`、`RAYTRAIN_TENANT_ISOLATION=strict`（多租户隔离，任务 9.3）。

### 3.3 给用户发 token（任务 9.1）

```bash
deploy/issue-token.sh alice --tenant team-a --days 30
# 产出 token-alice.txt（0600），通过安全渠道发给用户
```

---

## 4. Phase 2：用户接入（无需 kubeconfig，任务 8.x / 9.2）

```bash
raytrain configure        # Cluster mode 选 shared，填 server URL + token
# 在 ~/.raytrain/config.yaml 补 shared_clusters（运维提供 URL）：
#   shared_clusters:
#     h20:  http://ray-shared-h20-head.ray-shared.svc:8265
#     a100: http://ray-shared-a100-head.ray-shared.svc:8265

raytrain submit --config configs/x.py --gpus 8 --nodes 1 \
    --gpu-type h20 --cluster-mode shared --name exp1
raytrain logs <submission_id> -f --gpu-type h20
raytrain stop <submission_id> --gpu-type h20
raytrain list                       # [per-job] 与 [shared] 合并显示
```

应急回退：任意命令加 `--cluster-mode per_job`（需本机仍有 kubeconfig）。

---

## 5. 灰度与切换（运维，任务 10.x）

| 步骤 | 命令 / 文档 |
| --- | --- |
| 单 namespace 切默认 shared | `deploy/set-default-cluster-mode.sh shared --namespace <ns>` |
| 5 人 1–2 周灰度采集 | 模板 `docs/phase2-rollout.md`（成功率/启动时延/autoscaling/log 流） |
| 全量切换 + 停发 kubeconfig | 运行手册 `docs/phase2-cutover-runbook.md` |
| per_job 废弃节奏 | `docs/phase2-per-job-deprecation.md`（`RAYTRAIN_PERJOB_DEPRECATED=1` 开提醒） |

---

## 6. 验证与排障

```bash
# 全量单测（无需集群）
PYTHONPATH=. python3 -m pytest tests/ -q          # 期望全绿（203 passed）

# server 审计 / 成功率
kubectl -n raytrain-system logs deploy/raytrain-server --since=24h | grep job_submit
```

- 提交链路 / NCCL / Pending 排障：`docs/ops-guide.md` §6。
- 长寿集群 worker 扩不起来 / dashboard 不可达：`deploy/shared-cluster/README.md`。
- `--no-code-sync` 回退行为对照：`docs/phase1-no-code-sync-verification.md`。

---

## 7. 文档地图

| 主题 | 文档 |
| --- | --- |
| 用户使用（含代码同步 §9） | `docs/user-guide.md` |
| 快速开始 | `docs/quickstart.md` |
| 新仓库接入 + `.raytrainignore` | `docs/adding-new-repo.md` |
| 运维（镜像/数据/Code Bucket §9） | `docs/ops-guide.md` |
| 长寿集群部署/升级/排障 | `deploy/shared-cluster/README.md` |
| Phase 2 迁移（步骤/回退/FAQ） | `docs/migration-shared-cluster.md` |
| 灰度模板 P1 / P2 | `docs/phase1-rollout.md` / `docs/phase2-rollout.md` |
| 全量切换 + kubeconfig 退场 | `docs/phase2-cutover-runbook.md` |
| per_job 废弃计划 | `docs/phase2-per-job-deprecation.md` |
| 发布说明 | `docs/release-notes-phase1.md` |

> 注：所有需要真实 KubeRay/GPU/MinIO/MLflow 集群的步骤（冒烟训练、多周灰度、按月切换）
> 在无集群的本地环境中无法执行，已在各自文档里标注为 deferred，由运维在真实环境按手册执行；
> 所有代码级交付物均已实现并通过单测（`tests/` 全绿）。
