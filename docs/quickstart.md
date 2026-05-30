# 快速开始

5 分钟跑通第一个训练任务。

## 前提

运维已经完成（你不用管）：
- K8s 集群 + KubeRay Operator 已部署
- GPU 节点已打标签（`gpu=h20` / `gpu=a100`）
- 命名空间 `ray-cluster-3` 已创建，你有 RBAC 权限
- base 镜像 `training/base-raytrainv1.0.1-pytorch:ray2.54.1-torch2.5.0-cu124-raydata1.0` 已推送
- 项目镜像（如 `training/pointceptv1.0.1:ray2.54.1-torch2.5.0-cu124-raydata1.0`）已推送

## 第 1 步：安装 CLI

```bash
cd ~/raytrain
pip install -e .
raytrain --help
```

## 第 2 步：配置凭据（只做一次）

```bash
raytrain configure
```

按提示填：
- 用户名（如 `zhangsan`）
- 命名空间：`ray-cluster-3`
- MinIO 地址：`http://172.31.16.3:30950`
- MinIO access key / secret key
- MLflow 地址：`http://mlflow.mlflow.svc.cluster.local:5000`
- MLflow 用户名 / 密码

## 第 3 步：确认 kubectl 权限

```bash
kubectl -n ray-cluster-3 get rayjobs
```

不报 Forbidden 即可（空列表正常）。

## 第 4 步：进入项目目录

```bash
cd ~/pointcept-main    # 或你的训练项目目录
cat .raytrain.yaml     # 确认文件存在且 image 正确
```

## 第 5 步：提交任务

大多数原生 DDP 项目（如 Pointcept `tools/train.py`）提交方式不变：

```bash
raytrain submit \
    --config configs/scannet/semseg-pt-v3m1-0-base.py \
    --gpus 2 --nodes 1 --gpu-type h20 \
    --name my-first-run
```

默认会打包当前目录代码并上传 MinIO，集群侧通过 Ray `runtime_env.working_dir`
自动拉取，无需 build 镜像；改一行代码直接重新 `submit` 即可生效。如需回退到镜像
内置代码，加 `--no-code-sync`。（排除规则、200MiB 上限等细节见 `docs/user-guide.md`。）

如果项目的 `.raytrain.yaml` 使用 `launcher.type: ray_train`，提交命令也一样；
区别只在框架内部会先在 head 里启动一次 Ray Train driver，再由 `TorchTrainer`
自己调度 GPU worker。

## 第 6 步：查看状态和日志

```bash
raytrain list                    # 看任务状态
raytrain logs <job_name> -f      # 跟踪训练日志
raytrain exec <job_name>         # 进 Pod 调试
raytrain stop <job_name>         # 取消任务
```

## 成功标志

原生 DDP / `native_ddp` 项目日志中依次出现：
1. `[driver] node IPs: [...]` — GPU 节点分配成功
2. `[driver] syncing datasets...` — 数据同步中（首次慢，后续秒过）
3. `[node0] Start Training` — 训练开始
4. `[node0] step=... loss=...` — loss 在下降

Ray Train / `ray_train` 项目日志中依次出现：
1. `[driver] launcher type ray_train` — 进入 driver-side Ray Train 启动路径
2. `[ray-train] launching ... tools/train_ray.py ...` — head 中只启动一次训练 driver
3. `TorchTrainer` / Ray Train worker 日志 — GPU worker 由训练入口自己申请
4. `ray.data.read_lance` 或项目自己的 Ray Data 日志 — 数据源开始流式读取
5. `Start Training` / `step=...` — 训练进入实际 step

## 下一步

- 详细使用说明：`docs/user-guide.md`
- 接入新项目：`docs/adding-new-repo.md`
- 运维操作：`docs/ops-guide.md`
