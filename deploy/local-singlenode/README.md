# 单节点 k8s 快速验证（平台 UI + 后端）

在**一台单节点机器**上把平台跑起来，验证：登录 → 用户/配额管理 → 各页面点得通。
**不含 GPU 训练**（没有 RayCluster/MinIO/Postgres），只验证平台本身。
state 存内存，重启 pod 会清空——这是 smoke test，正常。

> 三类机器命令几乎一样，差别只在「把镜像喂给单节点」那一步。下面三种任选其一。

---

## 0. 前提

单节点机器上装好其中一套：

- **kind**（推荐，最干净）：需要 docker
- **k3s**：自带 containerd，最省事，但 load 镜像方式不同
- **minikube**：需要 docker/其它 driver

以及 `kubectl`、`docker`（k3s 可用其内置 `ctr`）。

把整个 repo（至少 `raytrain-server/`、`raytrain-web/`、`deploy/local-singlenode/`）拷到该机器。

---

## 1. 构建两个镜像（在单节点机器上）

```bash
cd <repo>/raytrain-server
docker build -t raytrain/raytrain-server:local -f Dockerfile .

cd ../raytrain-web
docker build -t raytrain/raytrain-web:local -f Dockerfile .
```

> web 的 `npm ci && npm run build` 在镜像内部跑，构建机不用装 node。

确认：
```bash
docker images | grep raytrain
# raytrain/raytrain-server  local ...
# raytrain/raytrain-web     local ...
```

---

## 2. 把镜像喂给单节点集群

清单里写的是 `imagePullPolicy: IfNotPresent` + 本地 tag（无 registry），
所以必须把镜像导进集群节点，否则会 ImagePullBackOff。

### 如果用 kind
```bash
kind load docker-image raytrain/raytrain-server:local
kind load docker-image raytrain/raytrain-web:local
```

### 如果用 minikube
```bash
minikube image load raytrain/raytrain-server:local
minikube image load raytrain/raytrain-web:local
```

### 如果用 k3s（镜像在 docker 里，需要导入 containerd）
```bash
docker save raytrain/raytrain-server:local | sudo k3s ctr images import -
docker save raytrain/raytrain-web:local    | sudo k3s ctr images import -
```

---

## 3. 部署平台

```bash
cd <repo>
kubectl apply -f deploy/local-singlenode/platform-local.yaml
kubectl -n raytrain-system rollout status deploy/raytrain-server
kubectl -n raytrain-system rollout status deploy/raytrain-web
kubectl -n raytrain-system get pods
```

两个 pod 都 `Running` / `READY 1/1` 即可。

---

## 4. 创建第一个管理员 token（一次性引导）

数据库（这里是内存）刚起来没有任何用户，先进后端 pod 签一个 admin token：

```bash
kubectl -n raytrain-system exec deploy/raytrain-server -- \
    raytrain-issue-token admin --role admin --days 30
```

输出第一行就是 token（`eyJ...`），复制下来。

> 因为 secret 里 JWT 密钥是固定值，pod 内签的 token 正好被同一个 server 接受。

---

## 5. 打开浏览器验证

拿到 web 的访问地址：

```bash
# kind / 通用：端口转发到本机（最稳）
kubectl -n raytrain-system port-forward svc/raytrain-web 8080:80
# 浏览器开 http://localhost:8080

# 或 minikube：
minikube service raytrain-web -n raytrain-system --url

# 或直接 NodePort（如果节点 IP 能访问）：
#   http://<节点IP>:30880
```

在登录页粘贴第 4 步的 token → 进入平台。

### 验证清单
- [ ] 登录成功，右上角显示 `admin (admin)`
- [ ] 左侧出现「用户管理」菜单（仅 admin 可见）
- [ ] 用户管理 → 创建用户：填配额（GPU 上限/并发任务）+ 授权 → 提交
- [ ] 创建成功弹出一次性 token
- [ ] 列表出现新用户；编辑改配额能保存；删除能成功
- [ ] 工作区 / 调试会话 / 提交训练 / 任务 / 数据集 页面都能打开
- [ ] 提交训练页顶部显示 GPU 配额横幅
  （注：本地没接 RayCluster，真正提交会报无可用集群，属预期）

---

## 6. 用新建的普通用户再验一遍（可选）

用第 5 步「创建用户」时弹出的 token，开一个新浏览器/无痕窗口登录：
- [ ] 看不到「用户管理」菜单（非 admin）
- [ ] 访问受限，符合权限设计

---

## 7. 清理

```bash
kubectl delete -f deploy/local-singlenode/platform-local.yaml
# kind 集群： kind delete cluster
```

---

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| pod `ImagePullBackOff` | 第 2 步没把镜像 load 进集群；或 tag 不是 `:local` |
| `/readyz` 不就绪 | 看日志 `kubectl -n raytrain-system logs deploy/raytrain-server` |
| 登录 401 | token 过期 / 不是这套 secret 签的；重跑第 4 步 |
| web 打不开后端 | nginx 反代目标是 `raytrain-server.raytrain-system.svc:8080`，本清单 Service 名一致，无需改 |
| 提交训练报错没有集群 | 预期：本地未接 RayCluster；要全链路见 `docs/platform-deploy.md` |
