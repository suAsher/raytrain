# raytrain submission server (FastAPI) image.
#
# 这个镜像是 **纯 Python**：只装 raytrain 框架 + server extra（fastapi + uvicorn）。
# 不含 torch / cuda / ray —— server 通过 HTTP 调 ray-shared 集群的 Job Submission
# API（8265 dashboard），ray 在 raytrain.server.ray_client 里是 lazy import，所以
# 镜像保持轻量。
#
# ── 构建 context ────────────────────────────────────────────────────────────
# build context 必须是 raytrain 仓库根目录（即包含 pyproject.toml 和 raytrain/ 的目录），
# 因为下面 `COPY . /opt/raytrain` 拷的是整棵源码树（.dockerignore 过滤 pycache/docs/tests 等）。
#
# 构建（在 raytrain/ 目录下执行）：
#   docker build \
#       -f deploy/server/Dockerfile \
#       -t 172.31.9.104:5050/raytrain/raytrain-server:0.1.0 \
#       .
#   docker push 172.31.9.104:5050/raytrain/raytrain-server:0.1.0
#
# 这个 tag 要和 deploy/server/deployment.yaml 里 Deployment.image 字段一致。
#
# ── 自检 ───────────────────────────────────────────────────────────────────
#   docker run --rm -p 8080:8080 \
#       -e RAYTRAIN_JWT_SECRET=dev-only-secret \
#       172.31.9.104:5050/raytrain/raytrain-server:0.1.0
#   # 另开一个终端：
#   curl -fsS http://127.0.0.1:8080/healthz   # 期望 {"status":"ok"}
#   curl -fsS http://127.0.0.1:8080/readyz    # 期望 {"status":"ready"}
#
FROM python:3.11-slim

# pip 镜像源，默认走清华 tuna（与 docs/ops-guide.md §4 一致）；
# 公网 / 其它内网构建时可 `--build-arg PIP_INDEX_URL=... --build-arg PIP_TRUSTED_HOST=...` 覆盖。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl 仅用于容器内 HEALTHCHECK 探活；slim 基础镜像默认没带。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/raytrain

# 先单独 COPY 依赖声明，最大化利用 layer 缓存：只改源码不改依赖时不会重装 pip 包。
COPY pyproject.toml README.md ./

# 把 raytrain 整棵源码树拷进来（context = 仓库根目录），再装 raytrain + server extra。
# `.[server]` = raytrain 核心依赖（pyyaml/click/jinja2/minio/boto3/...）+ fastapi + uvicorn[standard]。
COPY . /opt/raytrain
RUN python -m pip install --upgrade pip \
      --index-url ${PIP_INDEX_URL} \
      --trusted-host ${PIP_TRUSTED_HOST} \
    && python -m pip install \
      --index-url ${PIP_INDEX_URL} \
      --trusted-host ${PIP_TRUSTED_HOST} \
      --timeout 120 --retries 10 \
      ".[server]"

# 自检：FastAPI app + server 子模块能 import 才算构建成功（import 失败让 build 直接挂）。
RUN python -c "from raytrain.server.app import app; import fastapi, uvicorn; print('raytrain-server image OK, routes=', sorted({r.path for r in app.routes}))"

EXPOSE 8080

# 容器内探活：直接打 /healthz；k8s 侧另有 readiness/liveness probe（见 deployment.yaml）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# 监听 0.0.0.0:8080，Service / probe 都打这个端口。
CMD ["uvicorn", "raytrain.server.app:app", "--host", "0.0.0.0", "--port", "8080"]
