"""
Training job workbench: domain models, RayJob renderer, validation, state
aggregation, and API for the "training task workbench" product.

Layering (per spec):
    domain      — pure dataclasses / enums (TrainingJob intent, enums)
    labels      — centralized platform-reserved labels/annotations
    renderer    — TrainingJob intent -> KubeRay RayJob dict (+ Kueue labels)
    validate    — pre-submit validation (checkpoint/PVC/multi-node/reserved)
    state       — aggregate status from RayJob + Kueue + Pod + Events
"""
