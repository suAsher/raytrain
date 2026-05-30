#
# Make targets:
#   make install             在 Kasm 本地 pip install，供 CLI 使用
#   make test                跑渲染/launcher 单元测试（无需 K8s）
#   make image               在 BASE 之上构建出 BASE-raytrain 镜像
#   make push                docker push 上面的镜像
#   make rbac NS=ray-cluster-3   apply RBAC 和可选 Quota
#
REGISTRY     ?= 172.31.9.104:5050
REPO         ?= training/pointcept
BASE_TAG     ?= ray2.54.1-torch2.5.0-cu124
OUT_TAG      ?= $(BASE_TAG)-raytrain
BASE         := $(REGISTRY)/$(REPO):$(BASE_TAG)
OUT          := $(REGISTRY)/$(REPO):$(OUT_TAG)

.PHONY: help install test image push rbac clean

help:
	@echo "Targets:"
	@echo "  make install                   pip install -e .（本地 CLI 用）"
	@echo "  make test                      跑 tests/test_render.py"
	@echo "  make image   [BASE_TAG=...]    基于 \$$BASE 构建 \$$OUT 镜像"
	@echo "  make push                      docker push \$$OUT"
	@echo "  make rbac    [NS=...]          apply RBAC 和 Quota"
	@echo ""
	@echo "current:"
	@echo "  BASE = $(BASE)"
	@echo "  OUT  = $(OUT)"

install:
	pip install -e .

test:
	python tests/test_render.py

# 构建命令：build context 就是 raytrain/ 目录本身，
# Dockerfile.raytrain-layer 内的 COPY . /opt/raytrain 会把整份源码拷到镜像里。
image:
	docker build \
		-f deploy/Dockerfile.raytrain-layer \
		--build-arg BASE=$(BASE) \
		-t $(OUT) .
	@echo ""
	@echo "built: $(OUT)"
	@echo "next:  make push"

push:
	docker push $(OUT)

NS ?= ray-cluster-3
rbac:
	kubectl -n $(NS) apply -f deploy/rbac-raytrain-user.yaml
	kubectl -n $(NS) apply -f deploy/resource-quota.yaml

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -name __pycache__ -exec rm -rf {} +
