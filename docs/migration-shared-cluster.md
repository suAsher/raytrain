# 迁移到共享集群模式（Phase 2 · Shared_Cluster_Mode）

面向对象：平台运维 + 提交训练任务的同学。本文讲怎么把 raytrain 从 **per_job 模式**
（CLI 直连 K8s、每个任务现起一个 per-job RayCluster）迁移到 **shared 模式**
（CLI → raytrain Submission_Server → Ray Job Submission API → 长寿 RayCluster），
以及不兼容点、回退方法和常见问题。

对应 spec 任务 `long-term-evolution / 10.2`，依赖 Phase 2 的 8.x（CLI 适配
cluster_mode）与 9.x（token 颁发与多租户）。架构与落地节奏的权威来源是
`.kiro/specs/long-term-evolution/design.md`（见其 "Architecture" 与 "Migration Plan"
两节）。

> ⚠️ 重要前提：本文里凡涉及 `kubectl apply` / 部署长寿集群 / 部署 server 的步骤，都需要
> 一套带 KubeRay + GPU 的真实集群，本地无集群时只能做静态校验。实机部署与 1–2 周灰度
> 由运维在真实环境执行（数据采集模板见任务 10.3 的 `docs/phase2-rollout.md`）。

相关文件速查：

| 文件 / 脚本 | 作用 |
| --- | --- |
| `deploy/shared-cluster/raycluster-h20.yaml` / `raycluster-a100.yaml` | 长寿 RayCluster（namespace `ray-shared`，head Service `ray-shared-<type>-head:8265`） |
| `deploy/shared-cluster/README.md` | 长寿集群部署 / 升级（含 Ray 版本 drain）/ 排障 |
| `deploy/server/Dockerfile` / `deploy/server/deployment.yaml` | Submission_Server（namespace `raytrain-system`，ClusterIP 8080，Ingress） |
| `deploy/issue-token.sh` | 给单个用户签发 HS256 JWT（`sub`/`tenant`/`exp`），写 `token-<user>.txt`（0600） |
| `deploy/set-default-cluster-mode.sh` | 写 namespace ConfigMap `raytrain-defaults`（cluster-mode 解析的中间层） |
| `raytrain/cli/configure.py` / `raytrain/user_config.py` | `raytrain configure` 与 `~/.raytrain/config.yaml` 字段 |

---

## 1. 迁移步骤

迁移分**运维侧**（部署长寿集群 + server + 颁发 token）和**用户侧**（configure +
试跑），再走**灰度 → 全量**两步。下面命令均可复制粘贴，路径引用仓库内真实脚本/清单。

### 1.1 运维：创建 `ray-shared` namespace + 部署长寿 RayCluster

先建 namespace、给 GPU 节点打标签，再 apply 两份长寿集群清单。

```bash
# 1) namespace（幂等）
kubectl create namespace ray-shared || true

# 2) GPU 节点打 gpu=h20 / gpu=a100 标签（worker 的 nodeSelector 依赖它）
bash deploy/node-labels.sh
kubectl get nodes -L gpu        # 核对目标节点已有正确的 gpu 标签
```

> ⚠️ apply 前必须先改镜像：两份 YAML 里 head/worker 的 `image` 目前是**占位**
> （沿用项目镜像，注释里标了 `TODO(ops)`）。正式部署前换成专门的 **env-only 镜像**
> （只含 Ray + PyTorch + CUDA + 依赖，不含训练代码，代码在提交时由
> `runtime_env.working_dir` 从 MinIO 拉取）。构建方法见
> `deploy/Dockerfile.shared-cluster-env` 顶部注释。

```bash
# 3) 改完 image 后 apply（按需部署一个或两个 GPU 池）
kubectl apply -f deploy/shared-cluster/raycluster-h20.yaml
kubectl apply -f deploy/shared-cluster/raycluster-a100.yaml

# 4) 验证：两个集群都列出来，head pod Running（worker 此时为 0，正常）
kubectl -n ray-shared get raycluster -l raytrain.shared=true
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-h20
kubectl -n ray-shared get pods -l ray.io/cluster=ray-shared-a100

# 5) 确认 head Service / dashboard 可达（8265 = dashboard + Job Submission API）
kubectl -n ray-shared get svc ray-shared-h20-head ray-shared-a100-head
```

worker 组 `enableInTreeAutoscaling: true`、`minReplicas: 0`，空闲缩到 0 不占 GPU，
有任务投递时按需扩到 `maxReplicas: 16`。前置条件（hostPath 目录、KubeRay operator）和
更细的排障见 `deploy/shared-cluster/README.md`。

### 1.2 运维：部署 Submission_Server

构建并推送 server 镜像，创建 JWT 密钥与配置，apply 部署清单，最后经 Ingress 自测
`/healthz`。

```bash
# 1) 构建并推送镜像（build context = 仓库根目录，见 deploy/server/Dockerfile）
docker build -f deploy/server/Dockerfile \
    -t 172.31.9.104:5050/raytrain/raytrain-server:0.1.0 .
docker push 172.31.9.104:5050/raytrain/raytrain-server:0.1.0
```

> ⚠️ JWT 密钥一致性是迁移最容易踩的坑：**issue-token.sh 签名用的密钥**与 **server
> 验签用的密钥**必须是同一个值，否则用户 token 一律 401。`issue-token.sh` 默认读
> Secret `raytrain-jwt-key`，而 server Deployment 读 Secret `raytrain-server-secrets`
> 的 key `RAYTRAIN_JWT_SECRET`——两个 Secret 名字不同，所以要用**同一个值**创建两处。

```bash
# 2) 用同一个值创建两处 Secret（关键！）
VAL="$(openssl rand -hex 32)"
kubectl -n raytrain-system create secret generic raytrain-jwt-key \
    --from-literal=RAYTRAIN_JWT_SECRET="$VAL"
kubectl -n raytrain-system create secret generic raytrain-server-secrets \
    --from-literal=RAYTRAIN_JWT_SECRET="$VAL"
```

`RAYTRAIN_SHARED_CLUSTERS`（gpu_type → ray head dashboard URL 的 JSON）由
`deploy/server/deployment.yaml` 里的 ConfigMap `raytrain-server-config` 注入，默认值已
指向 1.1 部署的两个长寿集群；改 gpu_type / 集群入口时只改这个 ConfigMap，不动
Deployment。

```bash
# 3) apply 整套清单（Namespace / ConfigMap / Deployment / Service / Ingress）
#    注意：deployment.yaml 里自带一个占位 Secret stub，上一步已用真值覆盖即可；
#    Ingress 的 host / tls secretName / ingressClassName 是环境相关占位值，按 dev
#    集群实际改（见 deployment.yaml 文件末注释）。
kubectl apply -f deploy/server/deployment.yaml

# 4) 等 rollout，确认 ClusterIP:8080
kubectl -n raytrain-system rollout status deploy/raytrain-server
kubectl -n raytrain-system get svc raytrain-server

# 5) 集群内自测 /healthz（不依赖 Ingress）
kubectl -n raytrain-system run curl --rm -it --image=curlimages/curl --restart=Never -- \
    curl -fsS http://raytrain-server.raytrain-system.svc:8080/healthz   # 期望 {"status":"ok"}

# 6) 经 Ingress 的 HTTPS 自测（host 换成实际值）
curl -fsS https://raytrain.internal.example.com/healthz                  # 期望 200
```

> ⚠️ 安全提示：server 通过 Bearer token 鉴权，**务必经 Ingress 用 HTTPS 暴露**（或在
> NodePort 前加一层 TLS 终止）；明文暴露 8080 会让 token 在网络上裸奔。

### 1.3 运维：给白名单用户颁发 token

给 5 名白名单用户各签发一个 JWT。`issue-token.sh` 从 Secret 读 HS256 密钥，输出到
stdout 并写 `token-<user>.txt`（权限 0600）。`--tenant` 写入 token 的 `tenant` claim，
用于多租户隔离。

```bash
# 每个用户一条，--tenant 按团队/租户划分
deploy/issue-token.sh alice --tenant team-a --days 30
deploy/issue-token.sh bob   --tenant team-a --days 30
deploy/issue-token.sh carol --tenant team-b --days 30
deploy/issue-token.sh dave  --tenant team-b --days 30
deploy/issue-token.sh erin  --tenant team-b --days 30
```

把每个 `token-<user>.txt` 通过安全渠道发给对应用户（不要进 git / 群文件）。

### 1.4 用户：`raytrain configure` 切到 shared 模式

白名单用户在本机跑 `raytrain configure`，cluster-mode 选 `shared`，填 server URL 和
token。shared 模式**不需要 kubeconfig**。

```bash
raytrain configure
# 交互提示里：
#   Cluster mode ...................... shared
#   Platform server URL (shared mode) . https://raytrain.internal.example.com
#   Platform token (shared mode) ...... <粘贴 token-<user>.txt 的内容>
```

`shared_clusters`（gpu_type → ray head URL 映射）不在交互里 prompt，需手动编辑
`~/.raytrain/config.yaml` 补上（由平台运维提供 URL）：

```yaml
default_cluster_mode: shared
submission_server: https://raytrain.internal.example.com
token: <jwt>
shared_clusters:
  h20:  http://ray-shared-h20-head.ray-shared.svc:8265
  a100: http://ray-shared-a100-head.ray-shared.svc:8265
```

配好后在项目根目录试跑一个单卡冒烟任务（先 `--dry-run` 看一眼）：

```bash
cd ~/pointcept-main
raytrain submit --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 1 --nodes 1 --gpu-type h20 --name smoke-shared --dry-run
# 看着没问题再去掉 --dry-run 真提交
```

### 1.5 灰度 → 全量

按 `design.md` 的 "Phase 2 落地步骤"，先在 namespace 层面把默认模式切到 shared，跑
1–2 周稳定后再全量。`set-default-cluster-mode.sh` 写 namespace ConfigMap
`raytrain-defaults`，是 cluster-mode 解析的**中间层**（优先级：CLI flag > 本 ConfigMap >
用户配置 > per_job）。

```bash
# 灰度：把白名单用户所在 namespace 的默认模式切到 shared
deploy/set-default-cluster-mode.sh shared --namespace team-a

# 验证
kubectl -n team-a get configmap raytrain-defaults \
    -o jsonpath='{.data.default_cluster_mode}'
```

灰度 1–2 周，每天关注 submission 成功率、worker 启动时延、autoscaling 行为、log 流
稳定性（采集模板见 `docs/phase2-rollout.md`，任务 10.3）。稳定后扩大白名单直至全量
切换：

```bash
# 全量：逐个 namespace 切 shared
deploy/set-default-cluster-mode.sh shared --namespace <ns>
```

全量后**永久保留** `--cluster-mode per_job` 作为应急回退（见 §3）。再稳定 1 个月才停止
给新用户下发 kubeconfig（任务 10.5），6+6 个月后才废弃 per_job 路径（任务 10.6）。

---

## 2. 不兼容点

迁移到 shared 模式后，用户和运维需要知道以下具体差异。

### 2.1 任务标识：submission_id（shared）vs RayJob name（per_job）

- per_job 模式：任务标识是 **RayJob 名**（`<user>-<repo>-<exp>-<stamp>`），存在于
  namespace 里，`kubectl get rayjob` 可见。
- shared 模式：任务标识是 server 返回的 **submission_id**，由 Ray Job Submission API
  管理，K8s 里**没有**对应的 RayJob 对象。

`raytrain list` 会合并展示两类来源，并加前缀区分：`[per-job] <name>` 与
`[shared] <submission_id>`。

### 2.2 logs / stop 在 shared 模式需要 `--gpu-type`

submission_id 本身不携带它跑在哪个 GPU 池，而 server 的每个操作都按 gpu_type 路由到
对应长寿集群。所以 shared 模式下 `raytrain logs` / `raytrain stop` 建议显式带
`--gpu-type`：

```bash
raytrain logs  <submission_id> -f --gpu-type h20
raytrain stop  <submission_id>    --gpu-type h20
```

不带 `--gpu-type` 时 server 会**依次尝试每个已配置的 gpu_type** 直到命中（功能上可用，
但多一轮试探，显式指定更快、更确定）。CLI 里 `--gpu-type` 在 shared 模式默认 `h20`。

### 2.3 没有 per-job RayCluster；多任务共享长寿集群

shared 模式下提交**不再创建** per-job RayCluster。任务被投递到常驻的长寿集群，worker
由 autoscaler 按需 `0 → N` 扩缩。好处是省掉每次 1–2 分钟的集群启动开销；代价是 Ray
版本升级需要**先 drain**（停止接受新 submission → 等 in-flight 任务跑完 → rolling
update），详见 `deploy/shared-cluster/README.md` 的 "Ray 版本升级 · drain 步骤"。

### 2.4 kubeconfig：shared 不用，per_job 仍需

- shared 模式：本机**不需要** kubeconfig / kubectl，CLI 只通过 HTTPS + token 和 server
  通信（`_submit_shared` 提前返回，完全不触碰 K8s）。
- per_job 模式：仍然需要每用户的 kubeconfig，CLI 直连 K8s API。

因此即便全量切到 shared，只要还保留 per_job 回退能力，**per_job 用户的 kubeconfig 就仍
要可用**（kubeconfig 自然到期失效是更后面的任务 10.5）。

### 2.5 多租户隔离：strict 模式下跨租户操作被拒（403）

server 端 `RAYTRAIN_TENANT_ISOLATION=strict` 时：

- 提交任务时，token 的 `tenant` claim 被注入为任务环境变量 `RAYTRAIN_TENANT`
  （token 派生的 tenant 是权威值，用户无法用 `extra_env` 伪造）。
- 对**已存在任务**的 logs / stop / list，调用者的 tenant 必须与任务记录的 tenant 一致，
  否则返回 **403 `tenant_forbidden`**；list 还会额外过滤掉其它租户的任务。

> 默认 `RAYTRAIN_TENANT_ISOLATION` 为 `off`（向后兼容，单租户行为不变）。多租户部署需
> 在 server 显式设 `strict` 才生效。

### 2.6 MLflow tag 区分两类来源

MLflow run 上的 tag `raytrain.cluster_mode` 取值 `per_job` / `shared`，配合
`raytrain.gpu_type`、`raytrain.code_uri`、`raytrain.code_hash` 可区分并复现两类 run。

---

## 3. 回退方法

per_job 路径作为**永久应急回退**保留（`design.md` 规定 6+6 个月后才考虑废弃）。按影响
范围从小到大有三档手段。

### 3.1 单次命令回退（最快，用户侧）

在任意一条命令上加 `--cluster-mode per_job`，临时绕过 shared，走 K8s 直连路径：

```bash
raytrain submit --config <config> --gpus 8 --nodes 1 --cluster-mode per_job
raytrain list   --cluster-mode per_job
raytrain logs   <rayjob-name> -f --cluster-mode per_job
raytrain stop   <rayjob-name>    --cluster-mode per_job
```

CLI flag 是 cluster-mode 解析的最高优先级，会短路一切中间层（且不连 kube 去读
ConfigMap）。适合"个别用户 / 个别任务踩到 shared 故障"的临时绕行。前提：该用户本机仍有
可用 kubeconfig（见 §2.4）。

### 3.2 namespace 级回退（运维侧）

把整个 namespace 的默认模式切回 per_job，用户无需各自改本地配置：

```bash
deploy/set-default-cluster-mode.sh per_job --namespace <ns>

# 验证
kubectl -n <ns> get configmap raytrain-defaults \
    -o jsonpath='{.data.default_cluster_mode}'
```

适合"某 namespace 的 shared 链路整体异常"（如 server 故障、长寿集群升级窗口）。

### 3.3 用户级回退（用户侧，持久）

在 `~/.raytrain/config.yaml` 把默认模式改回 per_job：

```yaml
default_cluster_mode: per_job
```

该用户后续所有命令默认走 per_job，不必每次手敲 `--cluster-mode`。

> 三档的优先级关系：**CLI flag（§3.1）> namespace ConfigMap（§3.2）> 用户配置（§3.3）>
> 兜底 per_job**。所以即使 namespace 默认是 shared，用户仍可用 §3.1 临时回退单条命令；
> 即使用户配置是 shared，运维仍可用 §3.2 把整个 namespace 压回 per_job。

---

## 4. FAQ

**Q：我本机没有 kubeconfig，还能提交任务吗？**
A：shared 模式可以。shared 模式只需要 `submission_server` + `token`，CLI 通过 HTTPS 和
server 通信，完全不碰 K8s。只有 per_job 模式才需要每用户的 kubeconfig。

**Q：token 过期了怎么办？**
A：找运维用 `deploy/issue-token.sh <user> --tenant <id>` 重新签发一个，然后
`raytrain configure` 重新填 token（或直接编辑 `~/.raytrain/config.yaml` 的 `token`
字段）。token 过期时 CLI 会收到 server 的 401，提示重新配置。token 默认有效期 30 天，可
用 `--days` 调整。

**Q：提交后在哪看 dashboard / 任务详情？**
A：shared 模式下任务跑在长寿集群上，Ray dashboard 在对应 head Service 的 8265 端口。
本地可临时端口转发查看：

```bash
kubectl -n ray-shared port-forward svc/ray-shared-h20-head 8265:8265
# 浏览器打开 http://localhost:8265
```

日志直接用 `raytrain logs <submission_id> -f --gpu-type h20` 跟。MLflow 仍按
`~/.raytrain/config.yaml` 里的 `mlflow.tracking_uri`，不随 cluster_mode 变化。

**Q：shared 和 per_job 能并存吗？**
A：能。两套路径独立工作，`raytrain list` 会合并显示（前缀 `[per-job]` / `[shared]`
区分）。灰度期间同一用户既能跑 shared 又能用 `--cluster-mode per_job` 跑旧路径。

**Q：多机训练在 shared 模式怎么调度？**
A：靠长寿集群的 autoscaler。worker 组初始 0，提交多卡/多机任务时 autoscaler 按需把
worker 从 0 扩到所需数量（默认每 pod 1 GPU，最多 16）。如果要"整节点独占"语义
（大模型多机），运维需按 `deploy/shared-cluster/README.md` 里的说明把 worker 改成整节点
shape（`num-gpus: "8"` + `nvidia.com/gpu: "8"`，并把 `maxReplicas` 改成节点数）。

**Q：数据/代码同步在 shared 模式有变化吗？**
A：没有。Phase 1 的 code-as-submission 在两种模式下都生效：CLI 把当前目录打包成 zip 上传
（shared 模式经 server 的 `/v1/code` 上传），Ray 通过 `runtime_env.working_dir` 从 MinIO
拉取并解压，driver chdir 到解压目录。`--no-code-sync`（回退到镜像内代码）在两种模式下也
都可用。数据挂载 / Ray Data 配置保持一致。

**Q：配额和权限怎么控制？**
A：权限以 token 的 `sub`（用户）+ `tenant`（租户）claim 为准；server 设
`RAYTRAIN_TENANT_ISOLATION=strict` 时，跨租户的 logs/stop/list 会被拒（403），任务运行时
也会注入 `RAYTRAIN_TENANT`。GPU 配额由长寿集群的 `maxReplicas`（每池最多 16 GPU）做硬
上限。代码 zip 的存储则靠 `raytrain-code` bucket 的 7 天 lifecycle 兜底。

### 关于 design.md 的 Open Questions

`design.md` 列了几个实施前需确认的开放问题，迁移时按下面处理：

- **SSO / JWT（Open Question 2）**：当前 server 同时支持 raytrain 自签 HS256 JWT 和
  OIDC ID Token（`auth.py` 单一入口按 token header 的 `alg` 分流）。先用 `issue-token.sh`
  的自签 JWT 起步；公司 SSO 若只发 SAML，需要中间桥接服务再接入 OIDC。
- **Ray 版本升级 drain（Open Question 3）**：升级长寿集群前**先停止接受新 submission，
  等 in-flight 任务跑完再 rolling-update**，详细 drain 步骤见
  `deploy/shared-cluster/README.md`。两个池（h20 / a100）可分别 drain，降低停服面。
- **code bucket 配额（Open Question 4）**：`raytrain-code` 配 7 天 lifecycle 兜住总量；
  保守按每用户 5GiB（约 50 次 100MiB 提交）评估，超限报警。bucket 运维见
  `docs/ops-guide.md` 的 "Code Bucket 运维" 小节。
