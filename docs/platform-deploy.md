# raytrain 训练平台 · 部署指南（实测版）

本文教你把**完整训练平台**部署起来：用户打开浏览器 → 登录 → 建开发机 / 提交任务，
改完代码直接跑（code-as-submission），不写 YAML、不碰 kubectl。

> 本文的本地命令都已在本机实测通过（后端能起、能签 token、能鉴权；前端能 build）。
> 集群部署命令对应仓库里现成的清单。占位值（registry / namespace / MinIO 地址）按你环境替换。

---

## 1. 平台由三块组成（先搞清楚，别被 token 绕晕）

```
浏览器 ──登录(账号)──▶ raytrain-web (前端 SPA, React/AntD)
                          │ /v1/* 反代
                          ▼
                     raytrain-server (后端控制面, FastAPI)
                          │ Ray Job Submission API
                          ▼
                     长寿 RayCluster（KubeRay）──拉取──▶ MinIO 里的 code zip
```

| 组件 | 目录 | 作用 |
| --- | --- | --- |
| 前端 | `raytrain-web/` | 浏览器界面：登录、工作区、开发机、提交任务、任务列表、数据集 |
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
cd raytrain-web
npm install          # 首次
npm run dev          # vite dev server，默认 http://localhost:5173
```

浏览器打开 `http://localhost:5173`，登录页粘贴上面的 `$TOK` 即可进入界面。
（前端 `/v1` 请求要能到后端：dev 模式下配 vite 代理到 `127.0.0.1:8099`，
或先 `npm run build` 用 nginx 镜像跑——生产用第 4 节。）

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

### 4.2 前端 raytrain-web

前端镜像是**多阶段构建**：`npm ci && npm run build` 在镜像内部的 `node:20` 阶段
执行，产物拷进 nginx。所以**部署机不需要装 node/npm**，一条 `docker build` 即可。

```bash
cd raytrain-web
docker build -t 172.31.9.104:5050/raytrain/raytrain-web:v0.1 .   # 内部自动 npm build
docker push 172.31.9.104:5050/raytrain/raytrain-web:v0.1
```

> 前端的 K8s 清单（`raytrain-web/deploy/web.yaml`，NodePort 30880）已被
> `raytrain-server/deploy/kustomization.yaml` 收录，所以**不用单独 apply**——
> 上一步 §4.1 的 `kubectl apply -k deploy/` 已经把前端一起部署了。只需构建并推送镜像即可。

`raytrain-web` 的 nginx 把 `/v1` 反代到 `raytrain-server` 的 ClusterIP（见
`raytrain-web/nginx.conf`），所以前端只暴露一个入口。

### 4.3 给第一个用户发 token（一次性引导，仅此一次进 pod）

平台刚起来时数据库里还没有任何用户，无法在浏览器里登录去创建用户。所以**首次**用
`kubectl exec` 进后端 pod 签发一个**管理员** token：

```bash
kubectl -n raytrain-system exec deploy/raytrain-server -- \
    raytrain-issue-token alice --role admin --days 365
```

把 token 给 alice → 她浏览器打开 `http://<节点IP>:30880` → 登录页粘贴 token → 进平台。

> **此后不再进 pod。** alice（管理员）在浏览器「用户管理」里创建其他用户、分配配额与
> 授权、签发各自的 token。配额/权限修改即时生效，无需重发 token。

---

## 5. 最终效果（用户怎么用）

1. 浏览器打开平台地址，登录（粘贴 token，未来可接 SSO）。
2. **工作区 / 开发机**：申请一台带 GPU 的开发机调试代码（DevSessions 页）。
3. **提交任务**：Submit 页填训练意图（镜像/命令/GPU 数/数据集），平台自动打包当前代码 →
   上传 MinIO → 投递到长寿 RayCluster。**改完代码直接提交就能跑，不构建镜像。**
4. **任务列表 / 日志 / 数据集**：在界面看状态、日志、指标。
5. 管理员：配额、租户隔离、权限（token 的 role/tenant claim 控制）。

---

## 6. 验证与排障

```bash
# 后端单测（无需集群）
cd raytrain-server && pip install -e ".[dev]" && pytest tests/ -q

# 前端类型检查 + 构建
cd raytrain-web && npm run build

# 后端起不来 → 看日志
kubectl -n raytrain-system logs deploy/raytrain-server --tail=50
```

常见坑：
- **deployment 起不来 / 401 全员**：`raytrain-jwt-key` 还是 PLACEHOLDER，没用 `openssl rand` 覆盖。
- **签的 token 用不了**：签 token 用的密钥和 server 读的 `RAYTRAIN_JWT_SECRET` 不一致。
- **提交后任务不跑**：`RAYTRAIN_SHARED_CLUSTERS` 里的 head URL 不对，或长寿集群没起。
- **前端调不到后端**：nginx 反代目标或 vite 代理没指向 `raytrain-server`。

---

## 7. 与 CLI 路线的关系

`raytrain/`（CLI）和这套平台是**同一后端的两个入口**：
- 浏览器用户 → `raytrain-web` → `raytrain-server`
- CLI 用户 → `raytrain submit --cluster-mode shared` → 同一个 `raytrain-server`

两者都用 token 鉴权、都走 code-as-submission。**你要的"完整训练平台"= raytrain-server +
raytrain-web**，CLI 只是给习惯命令行的人留的旁路。CLI 侧的演进细节见
`docs/end-to-end-runbook.md` 与 `docs/migration-shared-cluster.md`。
