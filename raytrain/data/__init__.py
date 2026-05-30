"""
raytrain.data: Ray Data-backed dataset classes for training code.

Provides drop-in replacements for PyTorch datasets that leverage Ray Data
under the hood, enabling streaming from Lance/MinIO with zero-copy,
Plasma caching, prefetch, and CPU-offloaded transforms.

Usage in training code::

    from raytrain.data import RayLanceDataset, auto_dataset
"""
from .ray_lance_dataset import RayLanceDataset
from .env import auto_dataset

__all__ = ["RayLanceDataset", "auto_dataset"]
