"""Execution-profile capture for experiment metadata.

The package is deliberately dependency-downward: fingerprints do not import the
experiment façade, which keeps identity capture usable at daemon bootstrap.
"""

from .backends import model_profiles_from_backends

__all__ = ["model_profiles_from_backends"]
