from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GeoAnchor(BaseModel):
    latitude: float
    longitude: float


class GeoBounds(BaseModel):
    south: float
    west: float
    north: float
    east: float

    @model_validator(mode="after")
    def validate_order(self):
        if self.south >= self.north or self.west >= self.east:
            raise ValueError("Invalid geographic bounds")
        return self


class EnuPoint(BaseModel):
    x: float
    y: float
    z: float = 0.0


class SceneFeature(BaseModel):
    id: str
    category: Literal["building", "road", "terrain", "water"]
    footprint: list[EnuPoint] = Field(min_length=2)
    height: float = Field(default=0.0, ge=0.0)
    material: str = "itu_concrete"
    source: str = "user"
    osm_id: str | None = None
    osm_tags: dict[str, str] = Field(default_factory=dict)


class TerrainMesh(BaseModel):
    rows: int = Field(ge=2)
    columns: int = Field(ge=2)
    vertices: list[EnuPoint]
    faces: list[tuple[int, int, int]]
    elevation_offset_m: float
    resolution_m: float = Field(gt=0)
    source: str


class SceneRendering(BaseModel):
    renderer: str = "none"
    status: Literal["not_requested", "not_available", "rendered", "failed"] = "not_requested"
    osm_file: str | None = None
    model_file: str | None = None
    model_format: Literal["glb", "gltf", "obj"] | None = None
    asset_version: str | None = None
    message: str | None = None


class SceneModel(BaseModel):
    schema_version: int = 1
    name: str
    anchor: GeoAnchor
    bounds: GeoBounds | None = None
    size_x: float = Field(gt=0)
    size_y: float = Field(gt=0)
    features: list[SceneFeature] = Field(default_factory=list)
    terrain: TerrainMesh | None = None
    rendering: SceneRendering | None = None
