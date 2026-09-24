"""Candidate-conditioned root-cause and taxonomy diagnosis heads."""

from __future__ import annotations

import torch
from torch import Tensor, nn


class EventDiagnosisHeads(nn.Module):
    """Score legal root candidates and fault categories from event evidence."""

    def __init__(
        self,
        root_feature_count: int,
        event_feature_count: int,
        category_count: int,
        hidden_size: int = 64,
    ) -> None:
        super().__init__()
        if min(root_feature_count, event_feature_count, category_count, hidden_size) <= 0:
            raise ValueError("diagnosis head dimensions must be positive")
        self.root_feature_count = int(root_feature_count)
        self.event_feature_count = int(event_feature_count)
        self.category_count = int(category_count)
        self.hidden_size = int(hidden_size)
        self.candidate_projection = nn.Sequential(
            nn.Linear(self.root_feature_count, self.hidden_size),
            nn.GELU(),
            nn.LayerNorm(self.hidden_size),
        )
        self.event_projection = nn.Sequential(
            nn.Linear(self.event_feature_count, self.hidden_size),
            nn.GELU(),
            nn.LayerNorm(self.hidden_size),
        )
        self.root_head = nn.Sequential(
            nn.Linear(2 * self.hidden_size, self.hidden_size),
            nn.GELU(),
            nn.Linear(self.hidden_size, 1),
        )
        self.category_head = nn.Sequential(
            nn.Linear(2 * self.hidden_size, self.hidden_size),
            nn.GELU(),
            nn.Linear(self.hidden_size, self.category_count),
        )

    def forward(
        self,
        candidate_features: Tensor,
        event_features: Tensor,
        candidate_mask: Tensor,
    ) -> tuple[Tensor, Tensor]:
        if candidate_features.ndim != 3:
            raise ValueError("candidate_features must have shape [B, N, F_root]")
        if event_features.ndim != 2:
            raise ValueError("event_features must have shape [B, F_event]")
        batch, candidate_count, feature_count = candidate_features.shape
        if feature_count != self.root_feature_count:
            raise ValueError("candidate feature count does not match diagnosis head")
        if event_features.shape != (batch, self.event_feature_count):
            raise ValueError("event feature shape does not match diagnosis head")
        if candidate_mask.shape != (batch, candidate_count):
            raise ValueError("candidate mask must have shape [B, N]")
        mask = candidate_mask.to(dtype=torch.bool, device=candidate_features.device)
        if not bool(mask.any(dim=1).all()):
            raise ValueError("every event must have at least one legal candidate")
        if not torch.isfinite(candidate_features).all() or not torch.isfinite(event_features).all():
            raise ValueError("diagnosis features must be finite")

        candidate_hidden = self.candidate_projection(candidate_features)
        event_hidden = self.event_projection(event_features).unsqueeze(1)
        event_hidden = event_hidden.expand(-1, candidate_count, -1)
        joint = torch.cat((candidate_hidden, event_hidden), dim=-1)
        root_logits = self.root_head(joint).squeeze(-1)
        category_logits = self.category_head(joint)
        root_logits = root_logits.masked_fill(~mask, -torch.inf)
        category_logits = category_logits.masked_fill(~mask.unsqueeze(-1), -torch.inf)
        return root_logits, category_logits
