"""Publish generation projections from retained graphs or explicit parameter text."""
from __future__ import annotations

import json

from backend.metadata.generation import PARSE_VERSION, parse_generation
from backend.metadata.parameters import PARAMETERS_VERSION, parse_parameters


def publish_archived_generation(store, snapshot):
    """Publish generation and workflow classification from the same captured material."""
    result = _publish_archived_generation(store, snapshot)
    # F2：Workflow 分类与「生成阶段解析成功」是两件事，不能被后者的状态绑架。
    # 档案里明确没有生成材料时（not_available），仍能得出确定的分类 none，
    # project_workflow 本就支持 generation_result=None。早前这里对非 published
    # 一律提前返回，导致无参数 PNG 的 workflowClasses 永远是 0
    # （实测：三类材料里只有含 API prompt 的那 1 张拿到了分类）。
    #
    # 「失败/历史不覆盖成功投影」必须仍然成立，所以用**白名单**而不是黑名单：
    #   published     —— 生成解析成功，发布分类（修复前的原有行为，必须保留）；
    #   not_available —— 这份档案里确定没有可用的生成参数，是关于档案本身的
    #                   确定结论而非解析失败，同样应发布出 none 分类（F2 新增）。
    # 明确排除（黑名单侧）：rejected/failed（解析失败）、
    #   conflict / stale / idempotent（历史 run）—— 这些都不得触碰既有投影，
    #   tools/test-generation-pipeline-version.py 守的正是这条。
    if result.get('status') not in ('published', 'not_available'):
        return result
    from backend.metadata.workflow_projection import project_workflow
    from backend.metadata.workflow_projection_store import WorkflowProjectionStore
    generation = store.read_run(result['runUUID']) if result.get('runUUID') else None
    projection = project_workflow(snapshot, generation)
    if projection.get('classification') == 'unknown':
        # 材料不完整或歧义 —— 保守起见不动既有投影。
        return result
    workflow_store = WorkflowProjectionStore(store.database)
    workflow_store.initialize()
    workflow_store.publish(snapshot['uuid'], projection)
    return result


def _publish_archived_generation(store, snapshot):
    """Refuse ambiguous/invalid prompt blocks; never reopen the source file."""
    blocks = [chunk for chunk in snapshot['result'].get('text_chunks', [])
              if chunk.get('keyword') == 'prompt']
    from backend.metadata.video_candidates import generation_candidates
    video = generation_candidates(snapshot)
    video_parameters = [entry for entry in video if entry['key'] != 'workflow'
                        and any(line.startswith('Steps: ') for line in entry['text'].splitlines())]
    blocks.extend({'text': entry['text'], 'container_source': entry['source']}
                  for entry in video if entry['key'] == 'prompt' and entry not in video_parameters)
    if not blocks:
        candidates=[]
        candidates.extend((entry['text'], entry['source']) for entry in video_parameters)
        for index,chunk in enumerate(snapshot['result'].get('text_chunks',[])):
            if chunk.get('keyword')=='parameters' and not chunk.get('capture_error') and chunk.get('crc_status')=='valid':
                candidates.append((chunk.get('text'),{'kind':'png_parameters','chunkIndex':index}))
        for segment in [*snapshot['result'].get('segments', []),
                        *snapshot['result'].get('chunks', []),
                        *snapshot['result'].get('exif_chunks', [])]:
            if segment.get('type') == 'eXIf' and (segment.get('crc_status') != 'valid'
                    or segment.get('capture_error') or segment.get('status') != 'captured'):
                continue
            semantic=segment.get('semantics') or {}
            if semantic.get('status')=='ok' and semantic.get('source',{}).get('kind')=='exif_user_comment':
                candidates.append((semantic.get('text'),semantic['source']))
            elif segment.get('marker')=='FFFE' and isinstance(segment.get('text'),str):
                candidates.append((segment['text'],{'kind':'jpeg_comment','offset':segment.get('offset')}))
        candidates=[(text,source) for text,source in candidates if isinstance(text,str) and any(line.startswith('Steps: ') for line in text.splitlines())]
        if not candidates: return {'status':'not_available','reason':'no_supported_generation_parameters'}
        try:
            if len(candidates)!=1: raise ValueError('Multiple parameter sources require explicit selection')
            parsed=parse_parameters(*candidates[0])
        except (ValueError,TypeError,OverflowError) as error:
            return store.record_failure(snapshot['uuid'],PARAMETERS_VERSION,str(error))
        return store.publish(snapshot['uuid'],parsed)
    try:
        if len(blocks) != 1:
            raise ValueError('Multiple prompt blocks require explicit source selection')
        block = blocks[0]
        if block.get('capture_error') or (not block.get('container_source') and block.get('crc_status') != 'valid'):
            raise ValueError('Prompt block is incomplete or has invalid CRC')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON key in API prompt')
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError('Non-finite JSON value: ' + value)
        graph = json.loads(block['text'], object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
        if not isinstance(graph, dict) or not graph:
            raise ValueError('API prompt must be a nonempty node object')
        if any(not isinstance(node, dict) or not isinstance(node.get('class_type'), str)
               or not isinstance(node.get('inputs'), dict) for node in graph.values()):
            raise ValueError('Prompt is not a supported API node graph')
        parsed = parse_generation(graph)
        if block.get('container_source'):
            parsed['source'] = block['container_source']
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        return store.record_failure(snapshot['uuid'], PARSE_VERSION, str(error))
    # Storage failures propagate; they must not masquerade as parser failures.
    return store.publish(snapshot['uuid'], parsed)
