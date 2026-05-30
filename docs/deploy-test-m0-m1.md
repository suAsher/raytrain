# 部署测试手册（M0 + M1）

这份是「现在能不能部署测试」的完整答案。照着顺序走即可。

> 核心原则：**raytrain 烤进镜像，用户不 pip install**。
> 共三个镜像各烤一份 raytrain，用户只拿 token + 浏览器/CLI。

---

## 0. 当前状态

| 模块 | 代码 | 镜像 | 可测 |
|---|---|---|---|
| M0 代码免镜像 | ✅ | 需 build 训练镜像（含新 raytrain） | ✅ 镜像 build 后 |
| M1 控制面 | ✅ | 需 build `raytrain-server` | ✅ build 后 |
| M1 长寿 cluster | ✅ yaml | 需 build `raytrain-base-env` | ✅ build 后 |

**结论：先 build 3 个镜像，然后可以全链路测试。**

---

## 1. 三个镜像（raytrain 烤进去，用户零安装）

| 镜像 | Dockerfile | 烤进的 raytrain | 用途 |
|---|---|---|---|
| `raytrain-base-env` | `deploy/Dockerfile.shared-cluster-env` | driver + data（跑训练） | 长寿 cluster head/worker |
| `raytrain-server` | `raytrain-server/Dockerfile` | 不需要 client（独立 server） | 控制面 |
| 训练镜像（可选，旧 per_job 用） | `deploy/Dockerfile.raytrain-layer` | 全套 | 旧 per_job 模式兜底 |

### 1.1 build 长寿 cluster 镜像

```bash
cd <raytrain repo root>

docker build \
  -f deploy/Dockerfile.shared-cluster-env \
  --build-arg BASE=172.31.9.104:5050/training/base-ray-pytorch:ray2.54.1-torch2.5.0-cu124 \
  -t 172.31.9.104:5050/raytrain/raytrain-base-env:ray2.54.1-cu124-v1 \
  .

docker push 172.31.9.104:5050/raytrain/raytrain-base-env:ray2.54.1-cu124-v1
```

> BASE 换成你们真实的 base-ray-pytorch 镜像 tag。

### 1.2 build 控制面镜像

```bash
docker build \
  -t 172.31.9.104:5050/raytrain/raytrain-server:v0.1 \
  -f raytrain-server/Dockerfile \
  raytrain-server/

docker push 172.31.9.104:5050/raytrain/raytrain-server:v0.1
```

---

## 2. 准备 MinIO code bucket

```bash
MINIO_ENDPOINT=http://172.31.16.3:30950 \
MINIO_ACCESS_KEY=<ak> \
MINIO_SECRET_KEY=<sk> \
  deploy/setup-code-bucket.sh
```

验证：

```bash
mc ls raytrain-setup/raytrain-code
mc ilm export raytrain-setup/raytrain-code     # 应显示 7d 过期规则
```

---

## 3. 部署控制面 + 长寿 cluster

### 3.1 填密钥

编辑 `raytrain-server/deploy/secret-jwt-key.yaml`，替换两处 PLACEHOLDER：

```bash
openssl rand -hex 32         # → 填到 jwt_secret
# MinIO access_key / secret_key 填一个能写 raytrain-code 的账号
```

### 3.2 确认镜像 tag 一致

`raytrain-server/deploy/raycluster-shared-h20.yaml` 里的 image 要和 1.1 build 的一致：
```
image: 172.31.9.104:5050/raytrain/raytrain-base-env:ray2.54.1-cu124-v1
```

`raytrain-server/deploy/deployment.yaml` 里的 image 要和 1.2 一致：
```
image: 172.31.9.104:5050/raytrain/raytrain-server:v0.1
```

### 3.3 apply

```bash
kubectl apply -k raytrain-server/deploy/
```

### 3.4 验证控制面起来了

```bash
kubectl -n raytrain-system get pods
# raytrain-server-xxx   1/1  Running

# 探活
curl http://<任意节点IP>:30810/healthz
# {"status":"ok","version":"0.1.0"}
```

### 3.5 验证长寿 cluster 起来了

```bash
kubectl -n raytrain-shared get raycluster
# ray-shared-h20   ...

kubectl -n raytrain-shared get pods
# ray-shared-h20-head-xxx   1/1  Running
# (worker 此时 0 个，因为 autoscale min=0，正常)
```

---

## 4. 签发第一个 token

```bash
kubectl -n raytrain-system exec deploy/raytrain-server -- \
  raytrain-issue-token zhangsan --tenant occ --role user --days 365
# 输出一行 JWT（复制它）
```

---

## 5. 端到端测试一个真实训练

在**任意一台装了 raytrain CLI 的机器**（先临时本机装，M2 起会进 Workspace）：

```bash
# 5.1 配置（关键：cluster_mode = shared）
raytrain configure
#   user:               zhangsan
#   cluster_mode:       shared
#   submission_server:  http://<节点IP>:30810
#   token:              <上一步的 JWT>
#   minio endpoint/ak/sk: 填能访问 raytrain-code 的

# 5.2 验证 token 通
#   （任意能 curl 的地方）
curl -H "Authorization: Bearer <JWT>" http://<节点IP>:30810/v1/auth/me
# {"user":"zhangsan","tenant":"occ","role":"user",...}

# 5.3 进训练项目，dry-run 看会发什么
cd <你的训练项目>   # 里面有 .raytrain.yaml
raytrain submit --config configs/xxx.py --gpus 1 --gpu-type h20 --dry-run

# 5.4 真提交
raytrain submit --config configs/xxx.py --gpus 1 --gpu-type h20

# 5.5 看日志 / 列表 / 停止
raytrain logs <submission_id> -f
raytrain list --cluster-mode shared --gpu-type h20
raytrain stop <submission_id>
```

**验收标准**：
- working_dir 模式：head pod 日志能看到 `code-as-submission active`
- 长寿 cluster autoscale 起 worker
- read_lance 能读到数据，训练进第一个 step 出 loss

---

## 6. 关于「raytrain 不分发给用户」

| 角色 | 怎么拿到 raytrain | 用户要做的事 |
|---|---|---|
| 长寿 cluster | 烤进 `raytrain-base-env` 镜像 | 无 |
| 控制面 | 独立 `raytrain-server` 镜像 | 无 |
| 用户（M1 临时） | 暂时本机 pip（过渡） | configure 一次 |
| 用户（M2 起） | **烤进 Workspace 镜像** | 浏览器打开，零安装 |

**M1 阶段**用户机器还需要临时装一下 CLI（因为 Workspace 还没做）。
**M2 完成后**，用户连这一步都不用——打开浏览器进 Workspace，里面已经有 raytrain CLI + 4 种 IDE。

如果你想现在就让用户零安装，可以先跳过"用户本机装 CLI"，等 M2 的 Workspace 镜像做好，用户直接在浏览器开发机里 `raytrain submit`。

---

## 7. 故障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `curl /healthz` 连不上 | server pod 没起 / NodePort 错 | `kubectl -n raytrain-system logs deploy/raytrain-server` |
| submit 报 401 | token 过期 / secret 不一致 | 确认 server 的 jwt_secret 和签 token 时一致 |
| submit 报 `unsupported gpu_type` | configmap 里 shared_clusters 没配 h20 | 检查 `raytrain-server-config` configmap |
| head pod CrashLoop | env 镜像缺依赖 | `kubectl -n raytrain-shared logs <head-pod>` |
| 提交后 worker 一直 0 | autoscale 没触发 / GPU 不够 | `kubectl -n raytrain-shared describe raycluster` |
| read_lance 失败 | 镜像缺 pylance / MinIO 凭据没注入 | 进 worker pod 验证 `import lance` |
```
