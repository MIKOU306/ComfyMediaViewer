"""Conservative parser for ComfyUI API prompt graphs.

The parser extracts only values supported by known node contracts. It retains the
input graph verbatim and marks unknown reachable nodes as partial rather than
inventing prompt, model, or generation-lineage facts.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


PARSE_VERSION = 1
SAMPLERS = {"KSampler", "KSamplerAdvanced"}
OUTPUTS = {"SaveImage", "PreviewImage"}
TEXT_NODES = {"CLIPTextEncode", "CLIPTextEncodeSDXL"}
CONDITIONING_NODES = {
    "ConditioningCombine", "ConditioningConcat", "ConditioningAverage",
    "ConditioningSetArea", "ConditioningSetAreaPercentage", "ConditioningSetMask",
    "ConditioningZeroOut", "ConditioningSetTimestepRange",
    "ConditioningStableVideo",
}
KNOWN_GRAPH_NODES = {
    "EmptyLatentImage", "LatentUpscale", "LatentComposite", "LatentCompositeMasked",
    "RepeatLatentBatch", "ImageScale", "ImageBatch", "ImageCrop", "ImagePadForOutpaint",
    "ImageResize", "LoadImage", "CLIPLoader", "CLIPSetLastLayer", "ModelSamplingDiscrete",
}
CHECKPOINT_NODES = {"CheckpointLoaderSimple", "CheckpointLoader"}
UNET_NODES = {"UNETLoader"}
VAE_NODES = {"VAELoader", "VAEDecode", "VAEEncode", "VAEEncodeForInpaint"}
LORA_NODES = {"LoraLoader", "LoraLoaderModelOnly"}


def _node_class(node: Any) -> str:
    if not isinstance(node, Mapping):
        return ""
    return str(node.get("class_type") or "")


def _inputs(node: Any) -> Mapping[str, Any]:
    value = node.get("inputs", {}) if isinstance(node, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _link(value: Any) -> tuple[str, int] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2 and isinstance(value[0], (str, int)):
        return str(value[0]), int(value[1]) if isinstance(value[1], int) else 0
    return None


def _incoming(graph: Mapping[str, Any], node_id: str) -> list[tuple[str, str]]:
    result = []
    for name, value in _inputs(graph.get(node_id)).items():
        link = _link(value)
        if link:
            result.append((str(name), link[0]))
    return result


def _ancestors(graph: Mapping[str, Any], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen or current not in graph:
            continue
        seen.add(current)
        stack.extend(source for _, source in _incoming(graph, current))
    return seen


def _texts(graph: Mapping[str, Any], start: str) -> tuple[list[str], list[str]]:
    """Return exact encoder texts and unresolved node IDs from one conditioning input."""
    found: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        node = graph.get(current)
        if node is None:
            unresolved.append(current)
            continue
        kind = _node_class(node)
        if kind in TEXT_NODES:
            text = _inputs(node).get("text")
            if isinstance(text, str):
                found.append(text)
            else:
                unresolved.append(current)
            # Do not walk through CLIP input as if it were conditioning data.
            continue
        if kind not in CONDITIONING_NODES:
            unresolved.append(current)
        stack.extend(source for _, source in reversed(_incoming(graph, current)))
    return found, unresolved


def _model_records(graph: Mapping[str, Any], node_ids: set[str]) -> list[dict[str, Any]]:
    records = []
    for node_id in sorted(node_ids, key=lambda value: (len(value), value)):
        node = graph.get(node_id)
        kind, inputs = _node_class(node), _inputs(node)
        if kind in CHECKPOINT_NODES:
            records.append({"nodeId": node_id, "kind": "checkpoint",
                            "name": inputs.get("ckpt_name")})
        elif kind in UNET_NODES:
            records.append({"nodeId": node_id, "kind": "unet",
                            "name": inputs.get("unet_name"),
                            "weightDtype": inputs.get("weight_dtype")})
        elif kind == "LoraLoader":
            records.append({"nodeId": node_id, "kind": "lora",
                            "name": inputs.get("lora_name"),
                            "strengthModel": inputs.get("strength_model"),
                            "strengthClip": inputs.get("strength_clip")})
        elif kind == "LoraLoaderModelOnly":
            strength = inputs.get("strength_model", inputs.get("strength"))
            records.append({"nodeId": node_id, "kind": "lora",
                            "name": inputs.get("lora_name"),
                            "strengthModel": strength, "strengthClip": None})
        elif kind == "VAELoader":
            records.append({"nodeId": node_id, "kind": "vae",
                            "name": inputs.get("vae_name")})
    return records


def _sampler_model_records(graph: Mapping[str, Any], sampler_id: str) -> list[dict[str, Any]]:
    """Read model/clip/VAE ancestry in source-to-sampler loading order."""
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(node_id: str):
        if node_id in seen or node_id not in graph:
            return
        seen.add(node_id)
        node = graph[node_id]
        for key, value in _inputs(node).items():
            if key in {"model", "clip", "vae", "base_model", "base_clip"}:
                link = _link(value)
                if link:
                    visit(link[0])
        ordered.append(node_id)

    for key in ("model", "clip", "vae"):
        link = _link(_inputs(graph.get(sampler_id)).get(key))
        if link:
            visit(link[0])
    allowed = CHECKPOINT_NODES | UNET_NODES | VAE_NODES | LORA_NODES | {"CLIPLoader"}
    records = []
    for node_id in ordered:
        node = graph[node_id]
        if _node_class(node) in allowed:
            records.extend(_model_records(graph, {node_id}))
    return records


def parse_generation(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Parse a ComfyUI API-format prompt graph without mutating or normalizing it."""
    raw = deepcopy(graph) if isinstance(graph, Mapping) else graph
    if not isinstance(graph, Mapping):
        return {"parseVersion": PARSE_VERSION, "coverage": "partial",
                "warnings": ["prompt graph must be an object"], "steps": [],
                "prompts": [], "models": [], "outputs": [], "raw": raw}

    nodes = {str(key): value for key, value in graph.items()}
    warnings: list[str] = []
    output_nodes = [(node_id, _node_class(node)) for node_id, node in nodes.items()
                    if _node_class(node) in OUTPUTS]
    outputs = []
    steps = []
    prompt_records: list[dict[str, Any]] = []
    model_records: list[dict[str, Any]] = []
    unresolved_any = False

    if not output_nodes:
        warnings.append("no supported SaveImage or PreviewImage output node")

    for output_id, output_kind in output_nodes:
        reachable = _ancestors(nodes, output_id)
        sampler_ids = [node_id for node_id in nodes if node_id in reachable and
                       _node_class(nodes[node_id]) in SAMPLERS]
        output_step_ids = []
        output_unresolved = []
        for sampler_id in sorted(sampler_ids, key=lambda value: (len(value), value)):
            node = nodes[sampler_id]
            inputs = _inputs(node)
            step_reachable = _ancestors(nodes, sampler_id)
            positive: list[str] = []
            negative: list[str] = []
            unresolved: list[str] = []
            for field, destination in (("positive", positive), ("negative", negative)):
                link = _link(inputs.get(field))
                if not link:
                    if field in inputs:
                        warnings.append(f"sampler {sampler_id} has unrecognized {field} input")
                        unresolved.append(sampler_id)
                    continue
                texts, unresolved_nodes = _texts(nodes, link[0])
                destination.extend(texts)
                unresolved.extend(unresolved_nodes)
            step_models = _sampler_model_records(nodes, sampler_id)
            # The sampler's model/clip/vae ancestry is the only place model names are sourced.
            step = {
                "nodeId": sampler_id,
                "classType": _node_class(node),
                "seed": inputs.get("noise_seed", inputs.get("seed")),
                "steps": inputs.get("steps"),
                "cfg": inputs.get("cfg"),
                "sampler": inputs.get("sampler_name"),
                "scheduler": inputs.get("scheduler"),
                "positive": positive,
                "negative": negative,
                "models": step_models,
                "outputs": [output_id],
                "unresolvedNodes": sorted(set(unresolved)),
            }
            if _node_class(node) == "KSamplerAdvanced":
                for field in ("add_noise", "start_at_step", "end_at_step",
                              "return_with_leftover_noise"):
                    if field in inputs:
                        step[field] = inputs[field]
            steps.append(step)
            output_step_ids.append(sampler_id)
            output_unresolved.extend(unresolved)
            for role, texts in (("positive", positive), ("negative", negative)):
                for text in texts:
                    prompt_records.append({"stepId": sampler_id, "role": role, "text": text})
            model_records.extend(step_models)

        known = SAMPLERS | OUTPUTS | TEXT_NODES | CONDITIONING_NODES | CHECKPOINT_NODES | \
            UNET_NODES | VAE_NODES | LORA_NODES | KNOWN_GRAPH_NODES
        unsupported = sorted(node_id for node_id in reachable
                             if _node_class(nodes[node_id]) not in known)
        if unsupported:
            unresolved_any = True
            output_unresolved.extend(unsupported)
            warnings.append(f"output {output_id} reaches unsupported nodes: " +
                            ", ".join(unsupported))
        if output_unresolved:
            unresolved_any = True
        output_models: list[dict[str, Any]] = []
        for step_id in output_step_ids:
            matched = next(step for step in steps if step["nodeId"] == step_id)
            output_models.extend(matched["models"])
        already = {record["nodeId"] for record in output_models}
        for record in _model_records(nodes, reachable):
            if record["nodeId"] not in already:
                output_models.append(record)
                already.add(record["nodeId"])
        model_records.extend(output_models)
        out_inputs = _inputs(nodes[output_id])
        outputs.append({"nodeId": output_id, "classType": output_kind,
                        "filenamePrefix": out_inputs.get("filename_prefix"),
                        "steps": output_step_ids,
                        "models": output_models,
                        "reachableNodes": sorted(reachable, key=lambda value: (len(value), value)),
                        "unresolvedNodes": sorted(set(output_unresolved))})

    # A repeated sampler connected to multiple outputs remains one step with multiple bindings.
    by_step: dict[str, dict[str, Any]] = {}
    compact_steps = []
    for step in steps:
        current = by_step.get(step["nodeId"])
        if current is None:
            by_step[step["nodeId"]] = step
            compact_steps.append(step)
        else:
            current["outputs"].append(step["outputs"][0])
    unique_models = {record["nodeId"]: record for record in model_records}
    coverage = "empty" if not output_nodes else ("partial" if unresolved_any else "complete")
    return {"parseVersion": PARSE_VERSION, "coverage": coverage,
            "warnings": list(dict.fromkeys(warnings)), "steps": compact_steps,
            "prompts": prompt_records,
            "models": list(unique_models.values()), "outputs": outputs, "raw": raw}


__all__ = ["PARSE_VERSION", "parse_generation"]
