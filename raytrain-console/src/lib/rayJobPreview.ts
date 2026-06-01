// Pure render helper: turn a user's draft training config into a RayJob YAML
// preview (dry-run). This is NOT mock data — it's a deterministic projection of
// what the user typed, shown read-only in the Create-Job Review step so they
// can see roughly what the platform will submit. The authoritative manifest is
// always rendered server-side at submit time.

import type { Job } from "./types";

export function rayJobYaml(j: Pick<Job, "name" | "image" | "entrypoint" | "resources">): string {
  const { nodes, gpusPerNode, gpuType } = j.resources;
  return `apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: ${j.name}
  labels:
    raytrain.io/gpu-type: ${gpuType.toLowerCase()}
spec:
  entrypoint: ${j.entrypoint}
  runtimeEnvYAML: |
    working_dir: "s3://raytrain-code/${j.name}.zip"
  rayClusterSpec:
    headGroupSpec:
      rayStartParams: { num-gpus: "0" }
      template:
        spec:
          containers:
            - name: ray-head
              image: ${j.image}
    workerGroupSpecs:
      - groupName: gpu-workers
        replicas: ${nodes}
        rayStartParams: { num-gpus: "${gpusPerNode}" }
        template:
          spec:
            containers:
              - name: ray-worker
                image: ${j.image}
                resources:
                  limits: { nvidia.com/gpu: "${gpusPerNode}" }`;
}
