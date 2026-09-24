"""Trainable v2 diagnosis models."""

from .detector import ModelDimensions, MultiSourceDetector, self_supervised_loss
from .heads import EventDiagnosisHeads

__all__ = [
    "ModelDimensions",
    "MultiSourceDetector",
    "EventDiagnosisHeads",
    "self_supervised_loss",
]
