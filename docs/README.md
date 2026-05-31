# raytrain 文档导航（从这里开始）

这个仓库有两层东西，文档也按这两层组织：

1. **平台（Platform）** —— 浏览器训练平台：`raytrain-server`（后端）+ `raytrain-console`（前端）。
   用户登录网页 → 建开发机 / 提交任务 / 管理员管配额权限。**这是当前主线。**
2. **CLI** —— 命令行提交工具 `raytrain submit`，是平台的同源旁路（同一后端、同一
   code-as-submission），给习惯命令行的人用。

> **不知道看哪篇？** 按下面「我想做什么」对号入座即可，不用读全部 18 篇。

---

## 我想做什么 → 看这篇

| 你的目标 | 看这篇 | 说明 |
| --- | --- | --- |
| **把平台部署起来（生产/集群）** | [`platform-deploy.md`](platform-deploy.md) | ⭐ 主线。构建镜像 → apply → 引导首个 admin |
| **让训练真正跑到集群（Ray/RayData/Lance）** | [`platform-live-training.md`](platform-live-training.md) | ⭐ 配置共享集群 + code-as-submission + Lance 注入 + 开发机 |
| **单节点快速验证平台 UI** | [`../deploy/local-singlenode/README.md`](../deploy/local-singlenode/README.md) | 一台机器跑通登录/用户/配额，不含 GPU 训练 |
| **了解平台整体设计/架构** | [`raytrain-platform-proposal.md`](raytrain-platform-proposal.md) | 改造方案 v3（背景、架构、里程碑） |
| **第一次用 CLI 跑训练** | [`quickstart.md`](quickstart.md) | 5 分钟跑通第一个任务 |
| **日常用 CLI 提交/看日志/调试** | [`user-guide.md`](user-guide.md) | 建模同学日常手册 |
| **接入一个新训练项目** | [`adding-new-repo.md`](adding-new-repo.md) | 写 `.raytrain.yaml` / `.raytrainignore` |
| **运维平台/集群（命名空间、RBAC、镜像、排障）** | [`ops-guide.md`](ops-guide.md) | 平台运维手册 |
| **理解 code-as-submission（改代码不 build 镜像）** | [`code-as-submission.md`](code-as-submission.md) | 原理说明 |
| **Pointcept / sslod26 具体怎么提交** | [`pointcept-sslod26-raytrain-runbook.md`](pointcept-sslod26-raytrain-runbook.md) | 两个真实项目操作手册 |

---

## 文档全集（按类别）

### A. 平台（主线）
| 文档 | 用途 |
| --- | --- |
| [`platform-deploy.md`](platform-deploy.md) | **部署指南（实测版）**：本地预览 + 集群部署 + 首个 admin 引导 |
| [`platform-live-training.md`](platform-live-training.md) | **让训练真跑到集群**：共享集群配置、code-as-submission、Ray Data/Lance、开发机 |
| [`raytrain-platform-proposal.md`](raytrain-platform-proposal.md) | 平台改造方案 v3（架构 / 里程碑 / 设计决策） |
| [`../deploy/local-singlenode/README.md`](../deploy/local-singlenode/README.md) | 单节点快速验证（UI + 鉴权 + 用户/配额） |

### B. CLI 使用与接入
| 文档 | 用途 |
| --- | --- |
| [`quickstart.md`](quickstart.md) | 安装、配置、跑通第一个任务 |
| [`user-guide.md`](user-guide.md) | 日常提交、日志、exec、数据模式 |
| [`adding-new-repo.md`](adding-new-repo.md) | 新训练项目接入 |
| [`code-as-submission.md`](code-as-submission.md) | 代码即提交（working_dir）原理 |
| [`pointcept-sslod26-raytrain-runbook.md`](pointcept-sslod26-raytrain-runbook.md) | Pointcept + sslod26 操作手册 |
| [`sslod26-raydata-raytrain-before-after.md`](sslod26-raydata-raytrain-before-after.md) | sslod26 改造成 Ray Data/Lance 说明 |

### C. 运维
| 文档 | 用途 |
| --- | --- |
| [`ops-guide.md`](ops-guide.md) | 命名空间、节点标签、RBAC、镜像、数据迁移、排障 |
| [`migration-shared-cluster.md`](migration-shared-cluster.md) | per_job → shared 模式迁移 + 回退方法 |

### D. 历史 / 演进记录（一般不用看）
> 这些是 `long-term-evolution` spec 推进过程中按任务产出的**时间门槛运维计划与记录**
> （灰度、全量切换、废弃时间线等）。除非你在执行对应阶段的运维动作，否则可忽略。

| 文档 | 对应阶段 |
| --- | --- |
| [`deploy-test-m0-m1.md`](deploy-test-m0-m1.md) | M0/M1 部署测试（旧镜像路线） |
| [`end-to-end-runbook.md`](end-to-end-runbook.md) | spec 45 任务的端到端串讲 |
| [`phase1-rollout.md`](phase1-rollout.md) | Phase 1 灰度计划 |
| [`phase1-no-code-sync-verification.md`](phase1-no-code-sync-verification.md) | `--no-code-sync` 回退验证 |
| [`release-notes-phase1.md`](release-notes-phase1.md) | Phase 1 发布说明 |
| [`phase2-rollout.md`](phase2-rollout.md) | Phase 2 共享集群灰度 |
| [`phase2-cutover-runbook.md`](phase2-cutover-runbook.md) | Phase 2 全量切换 + kubeconfig 退场 |
| [`phase2-per-job-deprecation.md`](phase2-per-job-deprecation.md) | per_job 废弃与移除时间线 |

---

## 仓库结构速览

```text
raytrain/
├── raytrain/              # CLI（命令行提交工具）
├── raytrain-server/       # 平台后端（FastAPI 控制面）
│   └── deploy/            # 生产部署清单（kustomization）
├── raytrain-console/      # 平台前端（React + Tailwind 训练工作台）
│   └── deploy/            # 前端部署清单（web.yaml）
├── deploy/                # 集群基础设施脚本 + 共享集群清单
│   └── local-singlenode/  # 单节点快速验证包（本文 A 类）
├── docs/                  # 文档（本目录）
├── examples/              # 示例 .raytrain.yaml
└── tests/                 # CLI 测试
```

三个组件的关系：

```text
浏览器 ──▶ raytrain-console ──/v1──▶ raytrain-server ──Ray Job API──▶ 长寿 RayCluster
CLI    ──────────────────────────▶ raytrain-server ──┘（同一后端、同一 code-as-submission）
```
