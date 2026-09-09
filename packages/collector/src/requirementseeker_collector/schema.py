"""JSON Schema export from the models used at the Python boundary."""

from typing import Any, Literal

from pydantic import BaseModel

from .contracts import CollectionManifest, CollectionRecord, RawComment, RawVideo

ContractKind = Literal["video", "comment", "collection", "collection-manifest"]

_CONTRACT_MODELS: dict[ContractKind, type[BaseModel]] = {
    "video": RawVideo,
    "comment": RawComment,
    "collection": CollectionRecord,
    "collection-manifest": CollectionManifest,
}


def contract_model(kind: ContractKind) -> type[BaseModel]:
    return _CONTRACT_MODELS[kind]


def export_schema(kind: ContractKind) -> dict[str, Any]:
    return contract_model(kind).model_json_schema()
