"""Versioned identity hashes for bounded ComfyUI API prompt graphs.

These hashes describe saved parameters, not proof that a graph was executed.
UI workflows and unknown graph formats remain displayable candidates only.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .workflow import CANONICAL_VERSION, WorkflowFormatError, normalize_workflow

IDENTITY_ALGORITHM = "cmv-workflow-identity/1"
ADAPTER_VERSION = "comfyui-api-bindings/1"

# Each tuple is (input name, semantic binding type, accepted literal type).
_ADAPTERS = {
    "KSampler": (
        ("seed", "integer", "integer"), ("steps", "integer", "integer"),
        ("cfg", "float", "float"), ("sampler_name", "string", "string"),
        ("scheduler", "string", "string"), ("denoise", "float", "float"),
    ),
    "CLIPTextEncode": (("text", "text", "string"),),
    "CheckpointLoaderSimple": (("ckpt_name", "model_reference", "string"),),
    "LoraLoader": (
        ("lora_name", "model_reference", "string"),
        ("strength_model", "float", "float"), ("strength_clip", "float", "float"),
    ),
    "LoraLoaderModelOnly": (
        ("lora_name", "model_reference", "string"),
        ("strength_model", "float", "float"),
    ),
    "RandomNoise": (("noise_seed", "integer", "integer"),),
    "EmptyLatentImage": (
        ("width", "integer", "integer"), ("height", "integer", "integer"),
        ("batch_size", "integer", "integer"),
    ),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _hash(domain: str, graph_format: str, value: Any) -> str:
    namespace = f"{IDENTITY_ALGORITHM}\0{ADAPTER_VERSION}\0{CANONICAL_VERSION}\0{graph_format}\0{domain}\0"
    digest = hashlib.sha256(namespace.encode("utf-8") + _json(value).encode("utf-8")).hexdigest()
    return f"cmv:{domain}:{ADAPTER_VERSION}:{graph_format}:sha256:{digest}"


def _api_node_map(graph: Mapping[str, Any]) -> Mapping[str, Any]:
    nodes = graph.get("nodes")
    return nodes if isinstance(nodes, Mapping) else graph


def _is_link(value: Any) -> bool:
    """ComfyUI API links are two-item [source node id, output index] arrays."""
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], (str, int)) and not isinstance(value[0], bool)
            and type(value[1]) is int)


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return type(value) is int
    if expected == "float":
        return type(value) in (int, float) and math.isfinite(value)
    if expected in ("string", "text", "model_reference"):
        return isinstance(value, str)
    return False


def _literal_kind(value: Any) -> str:
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def analyze_workflow_identity(raw_workflow: Any, *, source_format: str | None = None) -> dict[str, Any]:
    """Analyze a graph without discarding raw execution data or claiming execution.

    Only bounded API prompt graphs receive comparable hashes. The strict archive
    normalizer validates UTF-8/JSON, duplicate keys, byte/depth/value budgets and
    format shape before this function processes the graph.
    """
    normalized = normalize_workflow(raw_workflow, source_format=source_format)
    graph_format = normalized["source_format"]
    graph = normalized["graph"]
    base = {
        "status": "unsupported",
        "coverage": "unsupported",
        "graph_format": graph_format,
        "algorithm": IDENTITY_ALGORITHM,
        "adapter_version": ADAPTER_VERSION,
        "canonical_version": normalized["canonical_version"],
        "execution_hash": None,
        "definition_hash": None,
        "structure_hash": None,
        "bindings": [],
        "warnings": [],
        "candidate_graph": graph,
        "hash_semantics": "Saved graph parameters only; hashes do not prove execution or output lineage.",
    }
    if graph_format != "comfyui-api":
        base["warnings"].append("Only ComfyUI API prompt graphs have typed identity adapters")
        return base

    nodes = _api_node_map(graph)
    wrapper_fields = ({key: value for key, value in graph.items() if key != "nodes"}
                      if isinstance(graph.get("nodes"), Mapping) else {})
    # Normalize the optional nodes wrapper to one canonical API node map. Its
    # unknown sibling fields remain part of identity and are not discarded.
    execution_graph = {"nodes": nodes}
    if wrapper_fields:
        execution_graph["wrapper_fields"] = wrapper_fields
    # Work only on the validated, normalized copy. Links and all unknown fields
    # remain unchanged in both execution and definition graphs.
    definition_graph = json.loads(_json(execution_graph))
    definition_nodes = definition_graph["nodes"]
    structure_nodes = []
    bindings = []
    definition_bindings = []
    warnings = []
    for raw_node_id in sorted(nodes, key=str):
        node_id = str(raw_node_id)
        node = nodes[raw_node_id]
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            warnings.append(f"Node {node_id} has no string class_type")
            structure_nodes.append({"node_id": node_id, "class_type": class_type,
                                    "inputs": _connections(node.get("inputs"))})
            continue
        adapter = _ADAPTERS.get(class_type)
        if adapter is None:
            warnings.append(f"Node {node_id} uses an unknown class_type: {class_type}")
        unknown_node_fields = set(node) - {"class_type", "inputs", "mode"}
        if unknown_node_fields:
            warnings.append(f"Node {node_id} has unadapted fields: {', '.join(sorted(unknown_node_fields))}")
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            warnings.append(f"Node {node_id} has invalid inputs")
            inputs = {}
        expected = {name: (binding_type, literal_type) for name, binding_type, literal_type in (adapter or ())}
        for input_name, value in inputs.items():
            if input_name not in expected:
                # Connections are always represented structurally and remain
                # untouched in definition/execution graphs.
                if adapter is not None and not _is_link(value):
                    warnings.append(f"Node {node_id} input {input_name} has no identity adapter")
                continue
            binding_type, literal_type = expected[input_name]
            # Connection arrays are graph structure, never literal runtime values.
            if _is_link(value):
                continue
            if not _matches_type(value, literal_type):
                warnings.append(f"Node {node_id} input {input_name} is not a {literal_type} literal")
                continue
            bindings.append({"node_id": node_id, "class_type": class_type,
                             "input": input_name, "type": binding_type, "value": value})
            definition_bindings.append({"node_id": node_id, "class_type": class_type,
                                        "input": input_name, "type": binding_type})
            definition_node = definition_nodes[raw_node_id]
            definition_node["inputs"][input_name] = {"$binding": input_name, "type": binding_type}

        structure_ports = {}
        for name, value in sorted(inputs.items(), key=lambda item: str(item[0])):
            key = str(name)
            declared = expected.get(name)
            literal_type = (declared[0] if declared and _matches_type(value, declared[1])
                            else _literal_kind(value))
            structure_ports[key] = ({"link": value} if _is_link(value)
                                    else {"literal_type": literal_type})
        structure_nodes.append({
            "node_id": node_id,
            "class_type": class_type,
            "mode": node.get("mode"),
            "inputs": structure_ports,
        })

    structure_graph = {"nodes": structure_nodes}
    if wrapper_fields:
        warnings.extend(f"Wrapper has unadapted field: {key}" for key in sorted(wrapper_fields))
    definition_identity = {"graph": definition_graph, "bindings": definition_bindings}
    base.update({
        "status": "comparable",
        "coverage": "complete" if not warnings else "partial",
        "execution_hash": _hash("workflow-execution", graph_format, execution_graph),
        "definition_hash": _hash("workflow-definition", graph_format, definition_identity),
        "structure_hash": _hash("workflow-structure", graph_format, structure_graph),
        "definition_graph": definition_graph,
        "definition_bindings": definition_bindings,
        "bindings": bindings,
        "warnings": warnings,
    })
    return base


