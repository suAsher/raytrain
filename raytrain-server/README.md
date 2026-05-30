# raytrain-server (Control Plane)

The raytrain Platform's HTTP API. Sits between users (CLI / Web UI) and the
KubeRay clusters; users no longer need a kubeconfig — they get a JWT, and the
server forwards their requests to a long-lived RayCluster via the Ray Job
Submission API.

## Layout

```
raytrain-server/
├── pyproject.toml                      installable as `raytrain-server`
├── Dockerfile                          multi-stage, slim runtime image
├── raytrain_server/
│   ├── main.py                         FastAPI app factory
│   ├── api/
│   │   ├── auth.py                     /v1/auth/me
│   │   ├── code.py                     PUT /v1/code (zip → MinIO)
│   │   ├── jobs.py                     POST/GET/DELETE /v1/jobs[/...]
│   │   └── health.py                   /healthz, /readyz
│   ├── core/
│   │   ├── settings.py                 Pydantic settings (env-driven)
│   │   ├── jwt_auth.py                 Issue + verify HS256 JWTs
│   │   ├── minio_client.py             Minio client factory
│   │   └── ray_client.py               Wrapper around JobSubmissionClient
│   └── scripts/
│       └── issue_token.py              `raytrain-issue-token` CLI
├── tests/                              55 unit + integration tests
└── deploy/
    ├── namespace.yaml                  raytrain-system + raytrain-shared
    ├── serviceaccount.yaml             SA + Role + RoleBinding
    ├── secret-jwt-key.yaml             placeholder for HMAC key + MinIO creds
    ├── configmap.yaml                  cluster URLs, MLflow URI, etc.
    ├── deployment.yaml                 single replica, RollingUpdate
    ├── service.yaml                    ClusterIP + NodePort 30810
    ├── raycluster-shared-h20.yaml      long-lived h20 RayCluster
    └── kustomization.yaml              one-shot `kubectl apply -k .`
```

## Quick start (local)

```bash
pip install -e .
RAYTRAIN_JWT_SECRET=$(openssl rand -hex 32) \
RAYTRAIN_SHARED_CLUSTERS='{"h20":"http://localhost:8265"}' \
RAYTRAIN_MINIO_ENDPOINT=http://localhost:9000 \
RAYTRAIN_MINIO_ACCESS_KEY=minio \
RAYTRAIN_MINIO_SECRET_KEY=minio12345 \
    uvicorn raytrain_server.main:app --reload
```

Issue a token:

```bash
RAYTRAIN_JWT_SECRET=$same raytrain-issue-token zhangsan --tenant occ --days 365
```

Curl it:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/v1/auth/me
```

## Deploy to K8s

1. Generate real secrets and replace `secret-jwt-key.yaml` placeholders:

   ```bash
   kubectl -n raytrain-system create secret generic raytrain-jwt-key \
       --from-literal=jwt_secret=$(openssl rand -hex 32) \
       --dry-run=client -o yaml > deploy/secret-jwt-key.rendered.yaml
   ```

2. Build + push the image:

   ```bash
   docker build -t 172.31.9.104:5050/raytrain/raytrain-server:v0.1 .
   docker push 172.31.9.104:5050/raytrain/raytrain-server:v0.1
   ```

3. Apply:

   ```bash
   kubectl apply -k deploy/
   ```

4. Issue the first token:

   ```bash
   kubectl -n raytrain-system exec deploy/raytrain-server -- \
       raytrain-issue-token alice --role admin --days 365
   ```

   Hand the token to alice; she configures her CLI:

   ```yaml
   # ~/.raytrain/config.yaml
   submission_server: http://<any-cluster-node>:30810
   token: eyJhbGc...
   ```

## Tests

```bash
pytest tests/ -v
```

55 tests: JWT (issue + verify + dependency injection), Ray client (cache,
runtime_env builder, submit forwarding), endpoints (auth, jobs CRUD, list
filtering, log streaming, code upload happy path + failure modes).
