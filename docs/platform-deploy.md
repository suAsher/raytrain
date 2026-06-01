# raytrain 训练平台 · 部署指南（实测版）

本文教你把**完整训练平台**部署起来：用户打开浏览器 → 登录 → 建开发机 / 提交任务，
改完代码直接跑（code-as-submission），不写 YAML、不碰 kubectl。

> 本文的本地命令都已在本机实测通过（后端能起、能签 token、能鉴权；前端能 build）。
> 集群部署命令对应仓库里现成的清单。占位值（registry / namespace / MinIO 地址）按你环境替换。

---

## 1. 平台由三块组成（先搞清楚，别被 token 绕晕）

```
浏览器 ──登录(账号)──▶ raytrain-console (前端 SPA, React/Tailwind 工作台)
                          │ /v1/* 反代
                          ▼
                     raytrain-server (后端控制面, FastAPI)
                          │ Ray Job Submission API
                          ▼
                     长寿 RayCluster（KubeRay）──拉取──▶ MinIO 里的 code zip
```

| 组件 | 目录 | 作用 |
| --- | --- | --- |
| 前端 | `raytrain-console/` | 浏览器训练工作台：Overview / Jobs / Create Job / Job Detail / Queues / Experiments / Artifacts / Datasets / Admin |
| 后端 | `raytrain-server/` | 鉴权、任务编排、调 KubeRay/MinIO/Ray、配额/租户隔离 |
| CLI（可选） | `raytrain/` | 命令行提交，给不想用浏览器的人 |

**token 是平台内部凭据，不是要发给每个用户手敲的东西。**
- 浏览器用户：在登录页**粘贴一次 token**（或未来接 SSO），之后都是点界面。
- token 取代的是"每人一个 kubeconfig"——这是它存在的唯一原因。
- 管理员用 `raytrain-issue-token` 给用户发 token，用户填进界面/CLI 一次即可。

---

## 2. 先决条件

- 一个 **K8s 集群**，已装 **KubeRay operator**（提供 RayCluster CRD）。
- 集群里已有 **MinIO**（存 code zip）和 **MLflow**（可选，记录实验）。
- 一个能 push 的 **镜像 registry**（如 `172.31.9.104:5050`）。
- 本机装了 `kubectl`、`docker`、`node`(≥18)、`python3`。

没有集群也能先在本机把后端+前端跑起来看界面（见第 5 节）。

---

## 3. 本地先跑通（**可选**，无需集群，5 分钟，确认东西是好的）

> 这一节是「没有集群时在本机看界面」的开发预览，**和部署集群无关**。
> 直接要上集群的，跳到第 4 节。这里的 `npm run dev` / 本地 uvicorn 都不是部署步骤。

### 3.1 起后端

```bash
cd raytrain-server
pip install -e .
RAYTRAIN_JWT_SECRET=dev-secret-please-rotate-32bytes-min \
RAYTRAIN_SHARED_CLUSTERS='{"h20":"http://localhost:8265"}' \
    python3 -m uvicorn raytrain_server.main:app --host 127.0.0.1 --port 8099
```

另开一个终端验证：

```bash
curl -fsS http://127.0.0.1:8099/healthz          # {"status":"ok","version":"0.1.0"}

# 签一个 token（密钥要和上面起服务用的一致）
TOK=$(RAYTRAIN_JWT_SECRET=dev-secret-please-rotate-32bytes-min \
  python3 -m raytrain_server.scripts.issue_token alice --tenant occ --days 1 | tail -1)

# 带 token 调鉴权接口
curl -fsS -H "Authorization: Bearer $TOK" http://127.0.0.1:8099/v1/auth/me
# {"user":"alice","tenant":"occ","role":"user",...}
```

> 注：本机没装 `ray` 也能起后端——只有真正提交任务到集群时才需要 ray，
> 本地看界面/鉴权不需要。

### 3.2 起前端

```bash
cd raytrain-console
npm install          # 首次
npm run dev          # vite dev server，默认 http://localhost:5174
```

浏览器打开 `http://localhost:5174`，登录页粘贴上面的 `$TOK` 即可进入界面。
（`vite.config.ts` 已把 `/v1` 代理到 `127.0.0.1:8099`，所以本地起了后端就能直接联调；
生产由 nginx 反代，见第 4 节。）

---

## 4. 部署到 K8s 集群（生产路径）

> **部署只有三步**，别被其它命令绕：
> - **① 构建镜像**：`docker build && push`（server 一个、web 一个；web 的 npm 在镜像内部跑，本机不用装 node）。
> - **② 部署**：建 secret → `kubectl apply -k deploy/`（server 全套）→ `kubectl apply -f deploy/web.yaml`（前端）。
> - **③ 引导一次**：`kubectl exec` 进后端 pod 签发**第一个管理员 token**（数据库里还没有用户，先有鸡才有蛋）。之后所有用户都在浏览器「用户管理」里建，**不再进 pod**。
>
> `npm` 属于①（已收进 Dockerfile）；`kubectl exec` 属于③（仅首次一次）。日常运维只有 ①+②。

### 4.1 后端 raytrain-server

```bash
cd raytrain-server

# (1) 构建并推送镜像
docker build -t 172.31.9.104:5050/raytrain/raytrain-server:v0.1 .
docker push 172.31.9.104:5050/raytrain/raytrain-server:v0.1

# (2) 先建 namespace（kustomization 里也有，但 secret 要先建）
kubectl create namespace raytrain-system || true

# (3) 创建真实密钥（替换 secret-jwt-key.yaml 里的 PLACEHOLDER）
kubectl -n raytrain-system create secret generic raytrain-jwt-key \
    --from-literal=jwt_secret="$(openssl rand -hex 32)" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n raytrain-system create secret generic raytrain-minio-creds \
    --from-literal=access_key="<minio-ak>" \
    --from-literal=secret_key="<minio-sk>" \
    --dry-run=client -o yaml | kubectl apply -f -

# (4) 按你的环境改 deploy/configmap.yaml（MinIO 地址、SHARED_CLUSTERS 的 head URL、MLflow URI）
#     和 deploy/deployment.yaml 里的 image tag。

# (5) 一键 apply 整个控制面（namespace/sa/secret/configmap/service/deployment/postgres + 长寿集群）
#     注意：secret 用上面的 create 命令覆盖了 placeholder；若你不想 apply 占位 secret，
#     可在 kustomization.yaml 注释掉 secret-jwt-key.yaml。
kubectl apply -k deploy/

# (6) 验证
kubectl -n raytrain-system rollout status deploy/raytrain-server
kubectl -n raytrain-system get svc raytrain-server-nodeport   # NodePort 30810
curl -fsS http://<任一节点IP>:30810/healthz
```

> 若集群还没有 h20 GPU 节点，先在 `deploy/kustomization.yaml` 注释掉
> `raycluster-shared-h20.yaml`，等集群就绪再单独 apply。

### 4.2 前端 raytrain-console

平台的 Web 前端是 **`raytrain-console`**（训练任务工作台：Overview / Jobs / Create Job
五步向导 / Job Detail 多 Tab / Queues / Experiments / Artifacts / Datasets / Admin）。
镜像是**多阶段构建**：`npm ci && npm run build` 在镜像内部的 `node:20` 阶段执行，产物
拷进 nginx。所以**部署机不需要装 node/npm**，一条 `docker build` 即可。

```bash
cd raytrain-console
docker build -t 172.31.9.104:5050/raytrain/raytrain-console:v0.1 .   # 内部自动 npm build
docker push 172.31.9.104:5050/raytrain/raytrain-console:v0.1
```

> 前端的 K8s 清单（`raytrain-console/deploy/web.yaml`，Deployment/Service 名仍为
> `raytrain-web`，NodePort 30880）已被 `raytrain-server/deploy/kustomization.yaml`
> 收录，所以**不用单独 apply**——上一步 §4.1 的 `kubectl apply -k deploy/` 已经把前端
> 一起部署了。只需构建并推送镜像即可。

`raytrain-console` 的 nginx 把 `/v1` 反代到 `raytrain-server` 的 ClusterIP（见
`raytrain-console/nginx.conf`），所以前端只暴露一个入口。

#### 页面与后端的对接情况
console 各页面**全部由后端真实接口驱动**（`/v1/console/*` + `/v1/auth`、`/v1/quota`、
`/v1/datasets`、`/v1/admin/*`），**无任何前端假数据**：
- **Overview / Training Jobs / Job Detail / Queues / Experiments / Artifacts**：读
  `/v1/console/*`，由 `raytrain-server` 的持久化 Store 提供。
- **Queues** 直接读集群 **真实 Kueue**（ClusterQueue/LocalQueue），不再有硬编码队列；
  集群读不到队列时显式报错，不回退假数据。
- **Job Detail · 日志**走 **Loki**（按 submission_id，结束后仍可查），**指标**走
  **Prometheus**，都带 `source` 标注；未配置/任务未真实提交时显示「不可用」而非伪造。
- **Job Detail · Pods/Events**：live 任务按 `ray.io/job-submission-id` 读**真实 Pod 与
  K8s 事件**（事件原因翻译为可读中文）；非 live 显式标注「未真实提交到集群」。
- **Artifacts**：从对象存储（MinIO/S3）按 job 的 checkpoint 前缀**真实列举**产物
  （checkpoint/model/log/eval 自动分类）；未配 MinIO 或非 `s3://` 路径时显式「不可用」。
- **Create Job**：项目/镜像下拉来自 `/v1/admin/resources/*`，队列候选来自真实 Kueue；
  无可用队列时**阻止提交**并提示。提交无集群的 gpu_type 会被后端拒绝（除非显式开
  `RAYTRAIN_ALLOW_RECORD_ONLY_SUBMIT`）。
- **开发机（Workspaces/DevSessions）**：状态由后端从 K8s **真实派生**（creating/starting/
  running/stopping/stopped/error + pod_phase + reason），**不再直接显示 running**；IDE/SSH
  入口仅在 `running` 时可点，否则禁用并提示；URL 由 NodePort 拼出（见 §8）。
- **登录 / 身份**：账号密码登录（JWT），`/v1/auth/me` + `/v1/quota`（顶栏配额）。
- **i18n**：顶栏「中文 / EN」一键切换，localStorage 持久化，默认中文，缺失键回退中文；
  后端 FriendlyError 按错误码本地化。
- 生产将 `RAYTRAIN_SEED_DEMO=false` 且 `RAYTRAIN_DATABASE_URL` 指向 Postgres，控制台
  只显示真实数据；列表为空时显示空状态（不再有示例 seed、不再有「演示数据」降级横幅）。

### 4.3 第一个管理员登录（账号密码，推荐）

平台支持**账号密码登录**：登录页默认就是用户名 + 密码（也保留「令牌登录」作为
自动化/CLI 旁路）。为了让全新平台开箱即有一个可登录的管理员，后端支持**引导管理员**——
通过环境变量在启动时自动创建一个 `admin` 账号：

```yaml
# 在 raytrain-server/deploy/configmap.yaml（或 Secret 更稳妥）里设置：
RAYTRAIN_BOOTSTRAP_ADMIN_USER: "admin"
RAYTRAIN_BOOTSTRAP_ADMIN_PASSWORD: "<改成强密码>"   # 建议放 Secret，不要明文进 ConfigMap
```

部署后浏览器打开平台 → 用 `admin` + 该密码登录 → 进入 **Admin · 用户** 创建其他用户
并为他们**设置密码**（用户即可账号密码登录）。**首次登录后请立刻改密码**，并把
`RAYTRAIN_BOOTSTRAP_ADMIN_PASSWORD` 从配置里移除（已存在的账号不会被重复创建）。

> 密码以 PBKDF2-HMAC-SHA256 加盐哈希存库（`users.password_hash`），不可逆、不回显。
> 登录成功后端签发 JWT，前端自动保存并在后续请求中带上——用户不再手贴 token。

#### 备选：CLI 签发令牌（自动化 / 不想用密码时）

```bash
kubectl -n raytrain-system exec deploy/raytrain-server -- \
    raytrain-issue-token alice --role admin --days 365
```

把 token 交给用户，在登录页切到「令牌登录」粘贴即可。

---

## 5. 最终效果（用户怎么用）

1. 浏览器打开平台地址，**账号密码登录**（管理员发的用户名/密码；自动化可用令牌登录）。
2. **Overview / Training Jobs**：看运行/排队/失败/成功概览与任务列表。
3. **Create Job**：5 步向导填训练意图（镜像/命令/GPU/数据集/挂载），实时资源估算 +
   校验；提交后落库为平台 job 记录。**改完代码直接提交就能跑，不构建镜像。**
4. **Job Detail**：状态时间线、日志、事件、Pods、指标、Config、Artifacts，可互相跳转。
5. **Queues / Experiments / Artifacts / Datasets**：队列资源、实验复现、产物、数据挂载。
6. 管理员：在 Admin 创建用户、设密码、分配 per-user 配额与授权（即时生效）。

---

## 6. 验证与排障

```bash
# 后端单测（无需集群）
cd raytrain-server && pip install -e ".[dev]" && pytest tests/ -q

# 前端类型检查 + 构建
cd raytrain-console && npm run build

# 后端起不来 → 看日志
kubectl -n raytrain-system logs deploy/raytrain-server --tail=50
```

常见坑：
- **deployment 起不来 / 401 全员**：`raytrain-jwt-key` 还是 PLACEHOLDER，没用 `openssl rand` 覆盖。
- **签的 token 用不了**：签 token 用的密钥和 server 读的 `RAYTRAIN_JWT_SECRET` 不一致。
- **提交后任务不跑**：`RAYTRAIN_SHARED_CLUSTERS` 里的 head URL 不对，或长寿集群没起。
- **前端调不到后端**：nginx 反代目标或 vite 代理没指向 `raytrain-server`。

---

## 7. 访问入口：NodePort（当前策略）与 HTTPS 演进

本期**统一用 NodePort 暴露入口**（不引入 Ingress），换取部署简单、少依赖：

| 入口 | Service | 端口 | 说明 |
| --- | --- | --- | --- |
| 控制台前端 | `raytrain-web`（console） | NodePort 30880 | 浏览器访问 `http://<节点IP>:30880` |
| 后端 API | `raytrain-server-nodeport` | NodePort 30810 | 健康检查/直连；前端 nginx 已反代 `/v1` |
| 开发机 IDE/SSH | `ws-<id>`（每台开发机一个 NodePort Service） | K8s 自动分配 | 后端读回 nodePort + 节点地址拼 URL |

**开发机 IDE/SSH 怎么连**：
- 后端创建开发机时建 `type: NodePort` 的 Service（Jupyter/code-server/PyCharm/SSH 四端口）。
- 状态变 `running` 后，后端用 `service_node_ports()` 读回每个端口的 nodePort，用
  `RAYTRAIN_WORKSPACE_NODE_HOST`（未配置则取 Pod 所在节点的 ExternalIP/InternalIP）拼出：
  - Jupyter：`http://<node>:<nodePort>/`
  - VS Code：`http://<node>:<nodePort>/`
  - SSH：`ssh://<node>:<nodePort>`
- 前端仅在开发机 `running` 时显示这些链接；未就绪显示「未就绪」提示（不给死链接）。
- 建议把 `RAYTRAIN_WORKSPACE_NODE_HOST` 设成一个稳定可达的节点地址（或 LB IP），
  避免 Pod 漂移到无外网 IP 的节点导致 URL 不可达。

> ⚠️ **JWT-over-HTTP 风险标注**：NodePort 是明文 HTTP，登录 JWT 在网络上**不加密传输**。
> 仅适用于**可信内网 / PoC**。生产对外暴露前必须上 TLS（见下）。

### HTTPS 演进（后续，分步可回退）

1. **加 Ingress + TLS**：部署 ingress-controller（如 ingress-nginx），为
   `raytrain-web` / `raytrain-server` 建 Ingress，用 cert-manager 签发证书，
   对外只暴露 443。前端 `/v1` 反代不变。
2. **开发机走 Ingress 子域**：把每台开发机的 IDE 从 NodePort 改为
   `https://ws-<id>.<base-domain>/...`（代码已有 `build_ide_urls(base_domain)` 备用路径，
   设 `RAYTRAIN_WORKSPACE_BASE_DOMAIN` 即可切换），需要泛域名证书 + 通配 DNS。
3. **回退排查**：若 Ingress 异常，临时回退到 NodePort（两套 Service 可并存），
   按上表用 `http://<节点IP>:300xx` 直连定位问题；确认 TLS 链路恢复后再摘除 NodePort。

---

## 8. 与 CLI 路线的关系

`raytrain/`（CLI）和这套平台是**同一后端的两个入口**：
- 浏览器用户 → `raytrain-console` → `raytrain-server`
- CLI 用户 → `raytrain submit --cluster-mode shared` → 同一个 `raytrain-server`

两者都用 token 鉴权、都走 code-as-submission。**你要的"完整训练平台"= raytrain-server +
raytrain-console**，CLI 只是给习惯命令行的人留的旁路。CLI 侧的演进细节见
`docs/end-to-end-runbook.md` 与 `docs/migration-shared-cluster.md`。
