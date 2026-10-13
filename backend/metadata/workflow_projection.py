"""Conservative workflow classification from already archived metadata."""
from __future__ import annotations

import json
from collections.abc import Mapping
from backend.metadata.archive import COLLECTOR_VERSION as PNG_COLLECTOR_VERSION

PROJECTION_VERSION = "workflow-projection/7"
_SUPPORTED_PNG_COLLECTOR = PNG_COLLECTOR_VERSION
_CLASSIFICATIONS = {"full", "prompt", "none", "unknown"}
_KINDS = {"t2i", "i2i", "cn", "other", "unknown"}


def project_workflow(snapshot_result, generation_result=None):
    """Project archive/generation records without reading the media file.

    ``snapshot_result`` accepts the archive snapshot wrapper (preferred) or its
    raw collector result. A bare result can prove positive evidence, but cannot
    prove that a missing payload means no generation metadata.
    """
    evidence = []
    snapshot, result = _snapshot_parts(snapshot_result)
    if result is None:
        return _projection("unknown", "unknown", ["snapshot_result_invalid"])
    if snapshot and snapshot.get('source_format') in ('mp4', 'ebml'):
        return _project_video(snapshot, generation_result)

    chunks = result.get("text_chunks")
    if not isinstance(chunks, list):
        chunks = []
        evidence.append("text_chunks_unavailable")

    workflow_chunks = [chunk for chunk in chunks if isinstance(chunk, Mapping)
                       and chunk.get("keyword") == "workflow"]
    prompt_chunks = [chunk for chunk in chunks if isinstance(chunk, Mapping)
                     and chunk.get("keyword") == "prompt"]
    parameter_chunks = [chunk for chunk in chunks if isinstance(chunk, Mapping)
                        and chunk.get("keyword") == "parameters"]

    # Repeated sources are ambiguous even if one happens to parse successfully.
    relevant_groups = (workflow_chunks, prompt_chunks, parameter_chunks)
    if any(len(group) > 1 for group in relevant_groups):
        return _projection("unknown", "unknown", ["duplicate_generation_source"])
    relevant = workflow_chunks + prompt_chunks + parameter_chunks
    if any(not _valid_chunk(chunk) for chunk in relevant):
        return _projection("unknown", "unknown", ["generation_source_incomplete_or_invalid"])

    generation = _successful_generation(generation_result)
    if workflow_chunks:
        graph = _parse_json(workflow_chunks[0].get("text"))
        if graph is None or not _is_ui_workflow(graph):
            return _projection("unknown", "unknown", ["workflow_json_invalid_or_not_ui_graph"])
        evidence.append("valid_comfyui_ui_workflow")
        # A co-captured API prompt is normal ComfyUI metadata. It carries
        # execution node types for the legacy kind rules while the UI graph is
        # still the evidence that makes this a full workflow.
        if prompt_chunks:
            api_graph = _parse_json(prompt_chunks[0].get("text"))
            if api_graph is None or not _is_api_graph(api_graph):
                return _projection("unknown", "unknown", ["co_captured_prompt_invalid"])
            if generation is None or not _is_successful_api_parse(generation):
                return _projection("full", "unknown", [*evidence, "api_graph_parse_not_successful"])
            if generation["raw"] != api_graph:
                return _projection("unknown", "unknown", ["api_parse_source_conflicts_with_snapshot"])
            kind, kind_evidence = _workflow_kind(api_graph)
            return _projection("full", kind, [*evidence, "successful_comfyui_api_graph_parse", *kind_evidence])
        kind, kind_evidence = _workflow_kind(graph)
        return _projection("full", kind, [*evidence, *kind_evidence])

    if prompt_chunks:
        graph = _parse_json(prompt_chunks[0].get("text"))
        if graph is None or not _is_api_graph(graph):
            return _projection("unknown", "unknown", ["prompt_json_invalid_or_unsupported_graph"])
        if generation is None or not _is_successful_api_parse(generation):
            return _projection("unknown", "unknown", ["api_graph_parse_not_successful"])
        if generation['raw'] != graph:
            return _projection("unknown", "unknown", ["generation_source_mismatch"])
        kind, kind_evidence = _workflow_kind(graph)
        evidence.extend(["successful_comfyui_api_graph_parse", *kind_evidence])
        return _projection("prompt", kind, evidence)

    if parameter_chunks:
        if generation is None or not _is_successful_parameter_parse(generation):
            return _projection("unknown", "unknown", ["parameter_parse_not_successful"])
        raw = generation['raw']
        source = {'kind': 'png_parameters', 'chunkIndex': chunks.index(parameter_chunks[0])}
        if raw.get('text') != parameter_chunks[0].get('text') or raw.get('source') != source:
            return _projection("unknown", "unknown", ["generation_source_mismatch"])
        return _projection("prompt", "unknown", ["successful_text_parameter_parse", "no_graph_type_evidence"])

    if generation is not None:
        if _is_successful_parameter_parse(generation) and _image_parameter_matches(result, generation['raw']):
            return _projection("prompt", "unknown", ["successful_archived_image_parameters", "no_graph_type_evidence"])
        # A published empty API parse is evidence of a parsed graph with no
        # supported outputs, not evidence that the file had no generation data.
        return _projection("unknown", "unknown", ["generation_exists_without_matching_snapshot_source"])
    if _collector_proves_absence(snapshot, result):
        return _projection("none", "unknown", ["complete_supported_png_capture_without_generation_chunks"])
    return _projection("unknown", "unknown", ["collector_does_not_prove_absence"])


def _project_video(snapshot, generation_result):
    from backend.metadata.video_candidates import generation_candidates, generation_key
    candidates = generation_candidates(snapshot)
    raw_candidates = snapshot['result'].get('text_candidates', [])
    relevant_count = sum(1 for entry in raw_candidates if isinstance(entry, Mapping)
                         and generation_key(entry) is not None)
    if relevant_count != len(candidates):
        return _projection('unknown', 'unknown', ['container_generation_material_invalid'])
    workflows = [entry for entry in candidates if entry['key'] == 'workflow']
    prompts = [entry for entry in candidates if entry['key'] == 'prompt'
               and not any(line.startswith('Steps: ') for line in entry['text'].splitlines())]
    if len(workflows) > 1 or len(prompts) > 1:
        return _projection('unknown', 'unknown', ['duplicate_container_generation_source'])
    generation = _successful_generation(generation_result)
    kind = 'unknown'
    if prompts:
        graph = _parse_json(prompts[0]['text'])
        if (graph is None or not _is_api_graph(graph) or generation is None
                or not _is_successful_api_parse(generation)
                or generation['raw'] != graph or generation.get('source') != prompts[0]['source']):
            return _projection('unknown', 'unknown', ['container_graph_source_unverified'])
        kind, _ = _workflow_kind(graph)
    if workflows:
        graph = _parse_json(workflows[0]['text'])
        if graph is None or not _is_ui_workflow(graph):
            return _projection('unknown', 'unknown', ['container_workflow_invalid'])
        return _projection('full', kind, ['archived_container_ui_workflow'])
    if prompts:
        return _projection('prompt', kind, ['archived_container_api_graph'])
    parameters = [entry for entry in candidates if entry['key'] != 'workflow'
                  and any(line.startswith('Steps: ') for line in entry['text'].splitlines())]
    if generation is not None and _is_successful_parameter_parse(generation) and len(parameters) == 1:
        raw = generation['raw']
        if (raw.get('text'), raw.get('source')) == (parameters[0]['text'], parameters[0]['source']):
            return _projection('prompt', 'unknown', ['archived_container_text_parameters'])
    return _projection('unknown', 'unknown', ['container_does_not_prove_generation_class'])


def _image_parameter_matches(result, raw):
    candidates = []
    for segment in [*result.get('segments', []), *result.get('chunks', []),
                    *result.get('exif_chunks', [])]:
        if not isinstance(segment, Mapping):
            continue
        if segment.get('type') == 'eXIf' and (segment.get('crc_status') != 'valid'
                or segment.get('capture_error') or segment.get('status') != 'captured'):
            continue
        semantic = segment.get('semantics') or {}
        if semantic.get('status') == 'ok' and semantic.get('source', {}).get('kind') == 'exif_user_comment':
            candidates.append((semantic.get('text'), semantic['source']))
        elif segment.get('marker') == 'FFFE' and isinstance(segment.get('text'), str):
            candidates.append((segment['text'], {'kind':'jpeg_comment', 'offset':segment.get('offset')}))
    candidates = [(text, source) for text, source in candidates if isinstance(text, str)
                  and any(line.startswith('Steps: ') for line in text.splitlines())]
    return len(candidates) == 1 and candidates[0] == (raw.get('text'), raw.get('source'))


def _snapshot_parts(value):
    if not isinstance(value, Mapping):
        return None, None
    if isinstance(value.get("result"), Mapping):
        return value, value["result"]
    return None, value


def _valid_chunk(chunk):
    return (not chunk.get("capture_error") and chunk.get("crc_status") == "valid"
            and isinstance(chunk.get("text"), str))


def _parse_json(text):
    if not isinstance(text, str):
        return None

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj

    try:
        value = json.loads(text, object_pairs_hook=unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _is_ui_workflow(graph):
    nodes = graph.get("nodes")
    return isinstance(nodes, list) and all(isinstance(node, dict) for node in nodes)


def _is_api_graph(graph):
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else graph
    return bool(nodes) and isinstance(nodes, dict) and all(
        isinstance(node, dict) and isinstance(node.get("class_type"), str)
        and isinstance(node.get("inputs"), dict) for node in nodes.values())


def _successful_generation(value):
    if not isinstance(value, Mapping) or value.get("status") != "successful":
        return None
    result = value.get("result")
    return result if isinstance(result, Mapping) else None


def _is_successful_api_parse(result):
    raw = result.get("raw")
    return (isinstance(raw, Mapping) and _is_api_graph(raw)
            and result.get("coverage") in ("complete", "partial", "empty"))


def _is_successful_parameter_parse(result):
    raw = result.get("raw")
    return (isinstance(raw, Mapping) and raw.get("format") == "a1111_parameters"
            and result.get("coverage") == "partial"
            and isinstance(result.get("steps"), list) and bool(result["steps"]))


def _collector_proves_absence(snapshot, result):
    if not snapshot:
        return False
    return (snapshot.get("source_format") == "png"
            and snapshot.get("collector_version") in (_SUPPORTED_PNG_COLLECTOR, 'raw-png-text/2')
            and not result.get('exif_chunks')
            and snapshot.get("state") in ("complete", "no_metadata")
            and snapshot.get("coverage") == "complete"
            and result.get("complete") is True
            and result.get("coverage") == "complete"
            and result.get("status") in ("complete", "no_metadata")
            and result.get("full_recovery") is True
            and not result.get("errors"))


def _workflow_kind(graph):
    """Match the legacy priority using explicit graph evidence only."""
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), (dict, list)) else graph
    if isinstance(nodes, list):
        records = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}
        class_of = lambda node: str(node.get("type") or node.get("class_type") or "").casefold()
        edges = {}
        links = graph.get("links")
        if isinstance(links, list):
            for link in links:
                if isinstance(link, list) and len(link) >= 4:
                    edges[str(link[0])] = str(link[1])
        def upstream(node, input_name):
            for item in node.get("inputs", []):
                if isinstance(item, dict) and item.get("name") == input_name and item.get("link") is not None:
                    source = edges.get(str(item["link"]))
                    if source in records:
                        return [source]
            return []
    elif isinstance(nodes, dict):
        records = {str(key): node for key, node in nodes.items() if isinstance(node, dict)}
        class_of = lambda node: str(node.get("class_type") or node.get("type") or "").casefold()
        def upstream(node, input_name):
            value = (node.get("inputs") or {}).get(input_name)
            return [str(value[0])] if isinstance(value, list) and value and str(value[0]) in records else []
    else:
        return "unknown", ["workflow_nodes_unavailable"]

    if not records:
        return "unknown", ["workflow_nodes_empty"]
    if any("controlnet" in class_of(node) for node in records.values()):
        return "cn", ["controlnet_node"]

    samplers = [(node_id, node) for node_id, node in records.items() if "sampler" in class_of(node)]
    found_empty = False
    for _, sampler in samplers:
        pending = upstream(sampler, "latent_image")
        seen = set()
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            node = records[node_id]
            kind = class_of(node)
            if "empty" in kind and "latent" in kind:
                found_empty = True
                return "t2i", ["sampler_latent_from_empty_latent"]
            if "vaeencode" in kind:
                return "i2i", ["sampler_latent_from_vae_encode"]
            if isinstance(node.get("inputs"), dict):
                for value in node["inputs"].values():
                    if isinstance(value, list) and value and str(value[0]) in records:
                        pending.append(str(value[0]))
            else:
                for input_record in node.get("inputs", []):
                    if isinstance(input_record, dict) and input_record.get("link") is not None:
                        source = _ui_link_source(graph, input_record["link"])
                        if source in records:
                            pending.append(source)

    has_image = any("loadimage" in class_of(node) for node in records.values())
    if has_image and not found_empty:
        return "i2i", ["load_image_without_empty_latent"]
    return "other", ["recognized_graph_without_legacy_kind_marker"]


def _ui_link_source(graph, link_id):
    links = graph.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, list) and len(link) >= 4 and str(link[0]) == str(link_id):
                return str(link[1])
    return None


def _projection(classification, kind, evidence):
    if classification not in _CLASSIFICATIONS:
        classification = "unknown"
    if kind not in _KINDS:
        kind = "unknown"
    return {"classification": classification, "kind": kind,
            "evidence": list(dict.fromkeys(evidence)),
            "projectionVersion": PROJECTION_VERSION}
