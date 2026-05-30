"""
Auto-configure a RayLanceDataset from ``RAYTRAIN_DATA_SOURCE_*`` env vars.

These env vars are injected by the raytrain driver when ``data_source:``
is configured in ``.raytrain.yaml``.

Usage in training code::

    from raytrain.data import auto_dataset

    ds = auto_dataset(
        transform_fn=MyAugmentActor,
        batch_size=6,
        materialize=True,
    )
    for batch in ds:
        train_step(batch)
"""
from __future__ import annotations

import os
from typing import Callable

from .ray_lance_dataset import RayLanceDataset


def auto_dataset(
    transform_fn: Callable | type | None = None,
    batch_size: int = 1,
    prefetch_batches: int = 4,
    local_shuffle_buffer_size: int = 512,
    materialize: bool = False,
    **kwargs,
) -> RayLanceDataset:
    """
    One-liner to create a RayLanceDataset from driver-injected env vars.

    All connection info (URI, version, filter, columns, S3 credentials)
    is read from ``RAYTRAIN_DATA_SOURCE_*`` and ``AWS_*`` env vars.

    Parameters
    ----------
    transform_fn : callable or class, optional
        Transform to apply via Ray Data ActorPool on CPU workers.
    batch_size : int
        Mini-batch size for iter_torch_batches().
    prefetch_batches : int
        Pipeline depth for prefetching.
    local_shuffle_buffer_size : int
        Window size for local shuffle.
    materialize : bool
        Whether to pin data in Plasma for multi-epoch reuse.

    Returns
    -------
    RayLanceDataset
        Ready-to-iterate PyTorch IterableDataset.

    Raises
    ------
    RuntimeError
        If ``RAYTRAIN_DATA_SOURCE_URI`` is not set.
    """
    uri = os.environ.get("RAYTRAIN_DATA_SOURCE_URI")
    if not uri:
        raise RuntimeError(
            "RAYTRAIN_DATA_SOURCE_URI not set. "
            "Are you running inside a raytrain job with data_source: configured?"
        )

    return RayLanceDataset(
        transform_fn=transform_fn,
        batch_size=batch_size,
        prefetch_batches=prefetch_batches,
        local_shuffle_buffer_size=local_shuffle_buffer_size,
        do_materialize=materialize,
        **kwargs,
    )
