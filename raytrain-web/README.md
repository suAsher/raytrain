# raytrain-web (Platform Console)

浏览器端控制台。用户打开它就是一个训练平台：创建工作区、申请 GPU 调试、
提交训练、看任务、管数据集。React + TypeScript + Ant Design。

## 页面

| 路由 | 页面 | 对应 API |
|---|---|---|
| `/login` | 粘贴 token 登录 | `GET /v1/auth/me` |
| `/workspaces` | 工作区：创建 / 4 IDE 入口 / start-stop-delete | `/v1/workspaces` |
| `/dev-sessions` | 申请 GPU 调试会话 / 终止 | `/v1/dev-sessions` |
| `/submit` | 提交训练（选数据集 / GPU / 命令） | `POST /v1/jobs` |
| `/jobs` | 任务列表 / 状态 / 停止 | `/v1/jobs` |
| `/datasets` | Lance 数据集注册 / 浏览 / schema | `/v1/datasets` |

## 本地开发

```bash
cd raytrain-web
npm install
# 把 API 指向运行中的 control plane（默认 localhost:8080）
RAYTRAIN_API=http://localhost:8080 npm run dev
# 打开 http://localhost:5173
```

## 构建 & 部署

```bash
docker build -t 172.31.9.104:5050/raytrain/raytrain-web:v0.1 .
docker push 172.31.9.104:5050/raytrain/raytrain-web:v0.1
kubectl apply -f deploy/web.yaml
# 浏览器打开 http://<节点IP>:30880
```

nginx 把 `/v1` + `/healthz` 反代到 `raytrain-server` Service，SPA 路由走
`try_files ... /index.html`。

## 类型约定

`src/api/types.ts` 镜像服务端 `raytrain_server/api/*.py` 的响应结构。
服务端改 schema 时同步这里。M5 后续可用 `openapi-typescript` 从 FastAPI
的 OpenAPI 自动生成，消除手工同步。
