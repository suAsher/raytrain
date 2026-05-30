# raytrain 多用户 RBAC 工具

这一套脚本和清单解决一个问题：**让每个 raytrain 用户拿到一份属于自己的
kubeconfig，且只能在管理员指定的 namespace 内操作 raytrain 相关资源**。

## 设计要点

| 需求                             | 实现                                          |
|---------------------------------|----------------------------------------------|
| 每人一份 kubeconfig              | 每个用户对应一个 namespace 内的 ServiceAccount |
| 只能在固定 namespace 操作        | RoleBinding 是 namespace-scoped，K8s 强制隔离 |
| 拥有 raytrain 所需的全部权限     | `role.yaml` 覆盖 submit / list / logs / exec / stop / data 用到的资源 |
| 管理员可以新建 / 变更 namespace  | `bootstrap-namespace.sh` 一条命令初始化新 ns |

> 安全边界由 K8s API server 强制：用户带着自己的 token 调任何 namespace 之外的
> API，K8s 直接返 403。kubeconfig 里的 `namespace:` 字段只是 kubectl 的默认
> 值，不是安全边界（这一点很关键）。

## 文件清单

```
deploy/rbac/
├── README.md                    # 本文件
├── role.yaml                    # raytrain-user Role 定义（每个 ns 一份）
├── resource-quota.yaml          # 可选 ResourceQuota（每个 ns 一份）
├── bootstrap-namespace.sh       # [admin] 初始化一个 namespace
├── add-user.sh                  # [admin] 给某用户开通某 namespace 的访问
├── rotate-token.sh              # [admin] 续发 token（不动 SA / RoleBinding）
├── remove-user.sh               # [admin] 撤销某用户的访问（token 立即失效）
└── list-users.sh                # [admin] 查看每个 ns 下的用户与配额状况
```

## 准备工作

1. 你（管理员）的 kubeconfig 至少有 `cluster-admin` 或等效权限：
   - `namespaces: create, get, label`
   - `roles, rolebindings, serviceaccounts, resourcequotas: create/update`
   - `tokens.serviceaccounts.io: create`（K8s ≥ 1.24）
2. 集群版本 K8s ≥ 1.24（`kubectl create token` 子命令需要这个版本）
3. 把脚本设为可执行：
   ```bash
   chmod +x deploy/rbac/*.sh
   ```

## 典型流程

### 1. 初始化一个 namespace（首次或新建）

```bash
# 复用一个已有 ns
./deploy/rbac/bootstrap-namespace.sh ray-cluster-3

# 或者新建一个 ns 并一并初始化
./deploy/rbac/bootstrap-namespace.sh new-team-a

# 不想要默认配额
./deploy/rbac/bootstrap-namespace.sh ray-cluster-3 --no-quota
```

幂等的：重复跑只会刷新 Role 和 Quota，不会出错。

### 2. 开通一个用户

```bash
./deploy/rbac/add-user.sh zhangsan ray-cluster-3
# 默认 token 1 年；要更短/更长：
./deploy/rbac/add-user.sh zhangsan ray-cluster-3 --duration 168h     # 7 天
./deploy/rbac/add-user.sh zhangsan ray-cluster-3 --duration 720h     # 30 天
```

输出：当前目录下生成 `kubeconfig-zhangsan-ray-cluster-3.yaml`，权限 `0600`。

发给本人，让他放到 `~/.kube/config` 或者 `export KUBECONFIG=...`。
用户验证：

```bash
kubectl get rayjobs                 # ok
kubectl get pods                    # ok
kubectl get pods -n other-ns        # 403 forbidden（这就是隔离边界）
raytrain submit ...                 # 现在可以用了
```

### 3. token 续期 / 重发

```bash
# 不影响 SA、RoleBinding，只换一份 token + kubeconfig
./deploy/rbac/rotate-token.sh zhangsan ray-cluster-3                    # 默认 1 年
./deploy/rbac/rotate-token.sh zhangsan ray-cluster-3 --duration 720h    # 自定义时长
```

旧 token 在它本身的 expiry 之前仍有效。要立刻让旧 token 失效，用下面的撤销
然后重新 add-user。

### 4. 撤销用户

```bash
# 单 ns
./deploy/rbac/remove-user.sh zhangsan ray-cluster-3

# 所有 raytrain 管理的 ns
./deploy/rbac/remove-user.sh zhangsan --all-namespaces
```

删除 ServiceAccount 会让该 SA 名下所有 token 立即失效（不论 expiry）。
该用户已提交的 RayJob / Secret / ConfigMap **不会被删**，需要时手工清理：

```bash
kubectl -n ray-cluster-3 delete rayjobs -l raytrain.owner=zhangsan
```

### 5. 查看现状

```bash
./deploy/rbac/list-users.sh                    # 全部
./deploy/rbac/list-users.sh ray-cluster-3      # 单 ns
```

输出包含：Role 是否存在、ResourceQuota 是否存在、每个 ns 当前有哪些用户。

## 给 raytrain CLI 用户的接入步骤

```bash
# 1. 收到管理员发的 kubeconfig 文件（假设保存在 ~/Downloads）
mkdir -p ~/.kube
cp ~/Downloads/kubeconfig-zhangsan-ray-cluster-3.yaml ~/.kube/config
chmod 0600 ~/.kube/config

# 2. 验证 K8s 通
kubectl get rayjobs

# 3. 配置 raytrain（这一步与 K8s 无关，是 raytrain 自己的配置）
raytrain configure
#   user_name:    填你和 add-user.sh 里一致的名字
#   namespace:    填这份 kubeconfig 对应的 namespace
#   minio:        管理员告诉你的 endpoint + access_key + secret_key
#   mlflow:       管理员告诉你的地址 + 账号

# 4. 提交训练
raytrain submit --config configs/xxx.py --gpus 1 --nodes 1 --name smoke
```

## 常见问题

### "为什么 kubeconfig 里写了 namespace 字段还要 RBAC？"
那个字段只是 kubectl 的默认值。安全边界由 RBAC 强制：用户调任何不在
binding 范围内的资源，K8s 直接 403。

### "用户能看到别人的 RayJob 吗？"
能 list 同 namespace 内的全部 RayJob（同 ns 同事互信，方便协作）。
跨 namespace 看不见。如果要严格按 owner 隔离，需要走 admission webhook，
当前脚本不覆盖这个层级。

### "用户能读到别人的 Secret（凭据）吗？"
当前 Role 给到 `secrets: list/get`，意味着同 namespace 内可以互看。
原仓库的 RBAC 也是这个粒度。要更严格的话需要做 admission webhook 或者
用 `resourceNames` 限定（K8s RBAC 不支持前缀匹配，需要精确名字，对动态
job_name 不实用）。短期建议：把同 ns 内用户当成可信团队。

### "用户离职怎么办？"
```bash
./deploy/rbac/remove-user.sh USERNAME --all-namespaces
```
他名下所有 token 立即失效。如果他还有跑着的 RayJob，按需清理。

### "K8s < 1.24 怎么办？"
旧版没有 `kubectl create token`，要走"给 SA 自动生成的长期 secret"那条路，
脚本里没实现。建议升级集群；实在不行单独跟我说。

### "我想给一个用户开通多个 namespace 怎么办？"
对每个 namespace 各跑一次 `add-user.sh`，会产出多份 kubeconfig：

```bash
./add-user.sh zhangsan ray-cluster-3
./add-user.sh zhangsan team-a
./add-user.sh zhangsan team-b
```

用户合并到一个 kubeconfig（可选）：

```bash
KUBECONFIG=kubeconfig-zhangsan-ray-cluster-3.yaml:kubeconfig-zhangsan-team-a.yaml:kubeconfig-zhangsan-team-b.yaml \
  kubectl config view --merge --flatten > ~/.kube/config
```

切换时用 `kubectl config use-context zhangsan@team-a`。raytrain 那边
通过 `~/.raytrain/config.yaml` 的 `namespace` 字段决定提交到哪个 ns。

### "我能不能把 Role 改成 ClusterRole 然后用 RoleBinding 引用？"
可以，效果等价（ClusterRole 经 RoleBinding 引用时，权限被限制在该 ns 内）。
当前用 Role 是为了让"权限是属于这个 ns 的"这件事在 manifest 上更直观；
如果你有几十个 ns 想统一管理，改成 ClusterRole 会少一份重复 apply。
改造思路：把 `role.yaml` 改成 `kind: ClusterRole`，`bootstrap-namespace.sh`
里把 `apply Role` 那步去掉，改成首次单独 apply 一次 ClusterRole。
