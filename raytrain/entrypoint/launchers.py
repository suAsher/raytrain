"""
Launcher adapters: turn a manifest's logical launcher description into the
concrete command to run on each node.

Each launcher takes a set of "slot values" (master_addr, master_port, node_rank,
world_size, num_gpus_per_node, num_nodes, config, save_path, run_id, workdir)
and returns a list[str] suitable for subprocess.
"""
from __future__ import annotations

from typing import Callable, Mapping

LauncherFn = Callable[[Mapping[str, str]], list[str]]


def _substitute(tpl: str, vals: Mapping[str, str]) -> str:
    # Simple {key} substitution. Unknown keys left as-is so users can pass
    # literal braces by double-braced them ({{ }}).
    class _D(dict):
        def __missing__(self, k):  # noqa: D401
            return "{" + k + "}"
    return tpl.format_map(_D(vals))


def native_ddp(manifest_launcher: dict, vals: Mapping[str, str]) -> list[str]:
    """
    Run the repo's own entrypoint as-is; all DDP bookkeeping is done by the
    repo itself via `--dist-url tcp://$MASTER_ADDR:$MASTER_PORT` and friends.
    """
    cmd = ["python", manifest_launcher["entrypoint"]]
    for a in manifest_launcher.get("args", []):
        cmd.append(_substitute(a, vals))
    return cmd


def torchrun(manifest_launcher: dict, vals: Mapping[str, str]) -> list[str]:
    """
    Wrap with `torchrun`. Repo's entrypoint only needs to call
    `torch.distributed.init_process_group("nccl")` — torchrun sets env vars.
    """
    cmd = [
        "torchrun",
        f"--nnodes={vals['num_nodes']}",
        f"--nproc_per_node={vals['num_gpus_per_node']}",
        f"--node_rank={vals['node_rank']}",
        f"--master_addr={vals['master_addr']}",
        f"--master_port={vals['master_port']}",
        manifest_launcher["entrypoint"],
    ]
    for a in manifest_launcher.get("args", []):
        cmd.append(_substitute(a, vals))
    return cmd


def accelerate(manifest_launcher: dict, vals: Mapping[str, str]) -> list[str]:
    """
    `accelerate launch --multi_gpu ...`. Same idea as torchrun.
    """
    cmd = [
        "accelerate", "launch",
        "--multi_gpu",
        f"--num_machines={vals['num_nodes']}",
        f"--num_processes={int(vals['num_nodes']) * int(vals['num_gpus_per_node'])}",
        f"--machine_rank={vals['node_rank']}",
        f"--main_process_ip={vals['master_addr']}",
        f"--main_process_port={vals['master_port']}",
        manifest_launcher["entrypoint"],
    ]
    for a in manifest_launcher.get("args", []):
        cmd.append(_substitute(a, vals))
    return cmd


def custom(manifest_launcher: dict, vals: Mapping[str, str]) -> list[str]:
    """
    Use `entrypoint` as the full command prefix; `args` are appended as-is
    (after substitution). The repo owns everything.
    """
    cmd = manifest_launcher["entrypoint"].split()
    for a in manifest_launcher.get("args", []):
        cmd.append(_substitute(a, vals))
    return cmd


def ray_train(manifest_launcher: dict, vals: Mapping[str, str]) -> list[str]:
    """
    Start a Ray Train/TorchTrainer driver once in the RayJob head pod.
    The entrypoint then schedules its own Ray Train workers, so it must not run
    inside the GPU-reserving NodeLauncher actor path.
    """
    entrypoint = manifest_launcher["entrypoint"]
    parts = entrypoint.split()
    if len(parts) == 1 and parts[0].endswith(".py"):
        cmd = ["python", parts[0]]
    else:
        cmd = parts
    for a in manifest_launcher.get("args", []):
        cmd.append(_substitute(a, vals))
    return cmd


LAUNCHERS: dict[str, LauncherFn] = {
    "native_ddp": native_ddp,
    "torchrun": torchrun,
    "accelerate": accelerate,
    "custom": custom,
    "ray_train": ray_train,
}


def build_command(launcher: dict, vals: Mapping[str, str]) -> list[str]:
    ltype = launcher.get("type", "native_ddp")
    if ltype not in LAUNCHERS:
        raise ValueError(f"unknown launcher type: {ltype}")
    return LAUNCHERS[ltype](launcher, vals)
