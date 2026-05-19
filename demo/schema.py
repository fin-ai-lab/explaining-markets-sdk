"""Pydantic mirror of the competition's OpenAPI schemas.

If the upstream OpenAPI spec changes (new fields, tightened types), update
the models below to match. Hand-maintained because there are only four.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AssetIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier_type: Literal["TICKER"]
    identifier_value: str


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    event_id: UUID
    event_type: str
    timing_category: Literal["SCHEDULED", "UNSCHEDULED"]
    event_datetime: datetime
    focal_assets: list[AssetIdentifier] = Field(min_length=1)
    information_url: str
    prediction_deadline: datetime


class AssetPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier_value: str
    predicted_percentile: float = Field(ge=0.0, le=1.0)


class PredictionSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    predictions: list[AssetPrediction] = Field(min_length=1)
