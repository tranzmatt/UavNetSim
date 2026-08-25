from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from scene.models import EnuPoint


class PlannerParameter(BaseModel):
    type: Literal["number", "integer", "boolean", "select"]
    label: str
    default: Any
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    options: list[Any] = Field(default_factory=list)


class PlannerMetadata(BaseModel):
    id: str
    name: str
    description: str
    parameters: dict[str, PlannerParameter]


class PlanningRequest(BaseModel):
    planner_id: str = "astar_3d"
    start: EnuPoint
    goal: EnuPoint
    uav_speed_mps: float = Field(default=10.0, gt=0, le=100)
    min_altitude_m: float | None = Field(default=None, ge=0, le=10000)
    max_altitude_m: float | None = Field(default=None, gt=0, le=10000)
    safety_clearance_m: float = Field(default=2.0, ge=0, le=1000)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request(self):
        if (
            self.min_altitude_m is not None
            and self.max_altitude_m is not None
            and self.min_altitude_m >= self.max_altitude_m
        ):
            raise ValueError("Maximum UAV altitude must be greater than minimum UAV altitude")
        if self.start == self.goal:
            raise ValueError("Start and goal must be different")
        return self


class PlanningResult(BaseModel):
    status: Literal["success"] = "success"
    planner_id: str
    path: list[EnuPoint]
    standard_metrics: dict[str, float | int]
    custom_metrics: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
