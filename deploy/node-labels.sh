#!/usr/bin/env bash
#
# Label GPU nodes so `raytrain submit --gpu-type <a100|h20>` can target them.
# Safe to re-run; --overwrite updates existing labels.
#
# Adjust the node names to whatever `kubectl get nodes` reports in your cluster.
#
set -euo pipefail

# A100 worker(s)
kubectl label node cactus gpu=a100 --overwrite

# H20 worker(s) — H20-2 is added once it joins rke2
kubectl label node H20 gpu=h20 --overwrite
kubectl label node H21 gpu=h20 --overwrite
# kubectl label node H20-2 gpu=h20 --overwrite   # enable when it joins

kubectl get nodes -L gpu
