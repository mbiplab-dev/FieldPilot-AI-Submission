"""Selectable detector checkpoints with pinned digests and verified downloads.

Named `models_registry`, not `models`: `models/` is the repo-root weights directory and a package
of that name would shadow it in path-relative contexts. Importing this package pulls in no ML
dependencies — `ultralytics`/`torch` load lazily inside `load_model` only.
"""

from fieldpilot.models_registry.registry import (
    CAPABILITIES,
    DEFAULT_MODELS_DIR,
    MODEL_OPTIONS,
    REQUIRED_FIELDS,
    ModelRegistryError,
    ensure_weights,
    ensure_weights_sync,
    file_sha256,
    get_option,
    list_models,
    load_model,
    load_model_sync,
    verify_local,
    verify_local_sync,
    weights_path,
)

__all__ = [
    "CAPABILITIES",
    "DEFAULT_MODELS_DIR",
    "MODEL_OPTIONS",
    "REQUIRED_FIELDS",
    "ModelRegistryError",
    "ensure_weights",
    "ensure_weights_sync",
    "file_sha256",
    "get_option",
    "list_models",
    "load_model",
    "load_model_sync",
    "verify_local",
    "verify_local_sync",
    "weights_path",
]
