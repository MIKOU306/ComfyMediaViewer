"""Conservative projection of retained A1111-style parameter text, without graphs."""
import math
import json
import re

PARAMETERS_VERSION = 5
MAX_LORA_REFERENCES = 1024
MAX_STAGES = 64

_LORA_TOKEN = re.compile(r'<(?P<family>lora|lyco):(?P<name>[^<>:]+):(?P<weight>[^<>]+)>')
_WEIGHT = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')

# A1111 参数文本里已知的扩展字段。它们只是「被识别出含义」的字段，
# **不代表我们声称识别了来源平台**（如 Forge）—— 见
# doc/ComfyUI-native媒体数据模型与迁移方案-2026-09-27.md 第174 行。
# 其余未知字段全部原样进入 platform_fields，不丢弃也不伪造。
_KNOWN_EXTENSIONS = frozenset({
    'Denoising strength',
    'Hires upscale',
    'Hires steps',
    'Hires sampler',
    'Hires CFG scale',
    'Hires prompt',
    'Hires negative prompt',
    'Refiner',
    'Refiner switch at',
    'TIme res',
    'Token merging',
    'Token merge ratio',
    'App',
    'AppVersion',
    'Backend',
    'User',
    'Date',
    'Version',
})


class _JSONPairs(list):
    """Distinguish JSON objects from arrays while retaining duplicate keys."""


def _lora_references(positive, negative, source, node_prefix='parameters'):
    references = []
    warnings = []
    ordinal = 0
    for side, prompt in (("positive", positive), ("negative", negative)):
        for match in _LORA_TOKEN.finditer(prompt):
            ordinal += 1
            if ordinal > MAX_LORA_REFERENCES:
                raise ValueError('LoRA reference count exceeds budget')
            raw_token = match.group(0)
            weight_raw = match.group("weight")
            if not _WEIGHT.fullmatch(weight_raw):
                warnings.append(f"Unparsed LoRA weight in {side} prompt at character {match.start()}")
                continue
            try:
                weight = float(weight_raw)
            except (ValueError, OverflowError):
                weight = math.inf
            if not math.isfinite(weight):
                warnings.append(f"Non-finite LoRA weight in {side} prompt at character {match.start()}")
                continue
            family = match.group("family")
            references.append({
                "nodeId": f"{node_prefix}:lora:{side}:{ordinal}",
                "kind": "lora",
                "name": match.group("name"),
                "referenceFamily": family,
                "referenceWeightRaw": weight_raw,
                "referenceWeight": weight,
                "strengthModel": None,
                "strengthClip": None,
                "source": {"kind": "a1111_lora_token", "side": side,
                           "rawToken": raw_token, "occurrence": ordinal,
                           "characterOffset": match.start(),
                           "characterLength": len(raw_token),
                           "parameterSource": source},
            })
    return references, warnings


def _hash_references(values, source):
    """Keep source-declared hashes explicitly unresolved and byte-for-text exact."""
    references = []
    model_name = values.get("Model")
    model_hash = values.get("Model hash")
    if model_hash:
        references.append({
            "kind": "checkpoint_hash_reference", "name": model_name,
            "hashAlgorithm": None, "hashValueRaw": model_hash,
            "assertion": "source_claim", "verification": "unverified",
            "source": {"kind": "a1111_parameter_field", "field": "Model hash",
                       "parameterSource": source},
        })

    lora_hashes = values.get("Lora hashes")
    if not lora_hashes:
        return references
    try:
        pairs = json.loads(lora_hashes, object_pairs_hook=_JSONPairs)
        if not isinstance(pairs, _JSONPairs):
            raise ValueError("Lora hashes must be a JSON object")
        for ordinal, (name, value) in enumerate(pairs, 1):
            if not isinstance(name, str) or not isinstance(value, str):
                references.append({
                    "kind": "lora_hash_reference", "name": name if isinstance(name, str) else None,
                    "hashAlgorithm": None, "hashValueRaw": value if isinstance(value, str) else None,
                    "assertion": "source_claim", "verification": "unverified",
                    "status": "malformed", "rawEntry": [name, value],
                    "source": {"kind": "a1111_parameter_field", "field": "Lora hashes",
                               "occurrence": ordinal, "parameterSource": source},
                })
                continue
            references.append({
                "kind": "lora_hash_reference", "name": name,
                "hashAlgorithm": None, "hashValueRaw": value,
                "assertion": "source_claim", "verification": "unverified",
                "status": "unverified",
                "source": {"kind": "a1111_parameter_field", "field": "Lora hashes",
                           "occurrence": ordinal, "parameterSource": source},
            })
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        references.append({
            "kind": "lora_hash_reference", "name": None,
            "hashAlgorithm": None, "hashValueRaw": lora_hashes,
            "assertion": "source_claim", "verification": "unverified",
            "status": "unparsed", "error": str(error),
            "source": {"kind": "a1111_parameter_field", "field": "Lora hashes",
                       "parameterSource": source},
        })
    return references

# 已被投影消费的字段；其余进入 platform_fields。
_CONSUMED_FIELDS = frozenset({
    'Steps', 'Seed', 'CFG scale', 'Sampler', 'Size', 'Model',
    'Lora hashes', 'Lora hashes',
})


def _parameter_fields(line):
    """Split one parameter line into an ordered key/value mapping."""
    fields=[]; token=[]; quoted=False; escaped=False; nesting=0
    for char in line:
        if escaped: token.append(char); escaped=False; continue
        if char=='\\' and quoted: token.append(char); escaped=True; continue
        if char=='"': quoted=not quoted
        if not quoted and char in '{[': nesting+=1
        elif not quoted and char in '}]': nesting-=1
        if char==',' and not quoted and nesting==0:
            fields.append(''.join(token).strip());token=[]
        else: token.append(char)
    if quoted or escaped: raise ValueError('Unterminated quoted parameter')
    if nesting!=0: raise ValueError('Unterminated structured parameter value')
    fields.append(''.join(token).strip())
    values={}
    for field in fields:
        key,sep,value=field.partition(':')
        key=key.strip();value=value.strip()
        if not sep or not key or key in values: raise ValueError('Invalid or duplicate parameter field')
        values[key]=value
    return values


def _split_stages(lines):
    """Split parameter text into ordered (prompt_text, parameter_line) stages.

    Writers that run more than one pass (base + upscale/refiner, or a Forge
    multi-stage chain) concatenate one full block per pass, each block ending in
    its own ``Steps:`` line, so earlier passes are *not* terminal.

    A multi-stage split is only accepted when the text is unambiguous:
      * the last ``Steps:`` line must still be the final line of the text;
      * every earlier ``Steps:`` line must have non-blank prompt text after it,
        otherwise we cannot tell a stage boundary from prompt prose that merely
        starts with "Steps: ".

    Anything else raises, so ambiguous text stays refused instead of being
    guessed into fabricated stages.
    """
    starts=[i for i,line in enumerate(lines) if line.startswith('Steps: ')]
    if not starts or starts[-1]!=len(lines)-1:
        raise ValueError('A terminal Steps parameter line is required')
    if len(starts)>MAX_STAGES:
        raise ValueError('Parameter stage count exceeds budget')
    stages=[]
    previous_end=0
    for position,start in enumerate(starts):
        block=lines[previous_end:start]
        if position>0:
            # Writers separate passes with blank lines; those separators are not
            # prompt text, so drop them instead of leaking a leading newline into
            # the next stage's positive prompt.
            while block and not block[0].strip():
                block=block[1:]
        prompt='\n'.join(block)
        if len(starts)>1 and not prompt.strip():
            # A real pass always carries prompt text of its own. An empty block
            # means we mis-detected a boundary (e.g. two bare "Steps:" lines), so
            # refuse rather than invent an empty stage. A single-stage text may
            # legitimately carry only a negative prompt or only parameters.
            raise ValueError('Parameter stage has no prompt text')
        if position<len(starts)-1:
            following=lines[start+1:]
            while following and not following[0].strip():
                following=following[1:]
            if not following or not any(line.strip() for line in following):
                raise ValueError('Ambiguous parameter stage boundary')
        stages.append((prompt,lines[start]))
        previous_end=start+1
    return stages


def _parse_stage(prompt, parameter_line, source, index, stage_count):
    """Project one stage. ``index`` keeps node identity unique across stages."""
    if prompt.count('\nNegative prompt: ')>1:
        raise ValueError('Ambiguous negative prompt sections')
    positive,separator,negative=prompt.partition('\nNegative prompt: ')
    if prompt.startswith('Negative prompt: '):
        positive,separator,negative='',True,prompt[len('Negative prompt: '):]
    values=_parameter_fields(parameter_line)
    node_id='parameters:0' if stage_count==1 else f'parameters:{index}'
    step={'nodeId':node_id,'classType':'ExternalParameters','source':source,
          'positive':[positive] if positive else [],'negative':[negative] if separator else [],'models':[]}
    for key,target in [('Steps','steps'),('Seed','seed')]:
        if key in values:
            if not re.fullmatch(r'-?\d+',values[key]): raise ValueError('Invalid '+key)
            if key=='Steps' and int(values[key])<0: raise ValueError('Negative Steps')
            step[target]=values[key] if key=='Seed' else int(values[key])
    if 'CFG scale' in values:
        cfg=float(values['CFG scale'])
        if not math.isfinite(cfg): raise ValueError('Non-finite CFG')
        step['cfg']=cfg
    if 'Sampler' in values: step['sampler']=values['Sampler']
    if 'Size' in values:
        match=re.fullmatch(r'(\d+)x(\d+)',values['Size'])
        if not match or any(int(v)<1 for v in match.groups()): raise ValueError('Invalid Size')
        step['width'],step['height']=map(int,match.groups())
    models=[]
    if values.get('Model'):
        models=[{'nodeId':f'{node_id}:model','kind':'checkpoint','name':values['Model'],
                 'source':{'kind':'a1111_parameter_field','field':'Model','parameterSource':source}}]
    lora_models, lora_warnings = _lora_references(positive, negative, source, node_id)
    models.extend(lora_models)
    step['models']=models
    hash_references = _hash_references(values, source)
    by_lora_name = {}
    for reference in hash_references:
        if reference.get('kind') == 'lora_hash_reference' and reference.get('name') is not None:
            by_lora_name.setdefault(reference['name'], []).append(reference)
    for model in lora_models:
        model['hashReferences'] = list(by_lora_name.get(model['name'], []))
    checkpoint_hash = next((ref for ref in hash_references
                            if ref['kind'] == 'checkpoint_hash_reference'), None)
    for model in models:
        if model['kind'] == 'checkpoint' and checkpoint_hash:
            model['hashReference'] = checkpoint_hash
    # 已知字段已被消费；其余字段原样提升到 platform_fields（版本化命名空间）。
    # 不声称来源平台，只标记「已知扩展」与「未识别字段」。
    platform_fields = {
        key: {'value': value, 'status': 'known' if key in _KNOWN_EXTENSIONS else 'unrecognized'}
        for key, value in values.items()
        if key not in _CONSUMED_FIELDS
    }
    unrecognized = [key for key, entry in platform_fields.items()
                    if entry['status'] == 'unrecognized']
    if unrecognized:
        lora_warnings.append(
            'Unrecognized parameter fields kept verbatim in platform_fields: '
            + ', '.join(sorted(unrecognized)))
    return step, models, hash_references, lora_warnings, platform_fields, values


def parse_parameters(text, source):
    if not isinstance(text,str) or len(text)>1024*1024:
        raise ValueError('Parameter text is invalid or exceeds budget')
    lines=text.splitlines()
    stages=_split_stages(lines)
    steps=[]; models=[]; hash_references=[]; stage_warnings_all=[]; stage_fields=[]
    platform_fields={}
    raw_fields={}
    for index,(prompt,parameter_line) in enumerate(stages):
        step,stage_models,stage_hashes,stage_warnings,stage_platform,values=\
            _parse_stage(prompt, parameter_line, source, index, len(stages))
        steps.append(step)
        models.extend(stage_models)
        hash_references.extend(stage_hashes)
        stage_warnings_all.extend(stage_warnings)
        # Keep the classified extension map per stage; ``raw.fields`` keeps the
        # verbatim values so原文 stays unmodified.
        stage_fields.append(stage_platform)
        if index==0:
            raw_fields=values
        # Multi-stage text: extension fields differ per pass, so the top-level map
        # is only meaningful for a single stage. With several stages we keep them
        # per stage and leave the top level empty, so a later pass can never
        # silently overwrite an earlier one.
        if len(stages)==1:
            platform_fields=stage_platform
    warnings = ['Text parameters do not prove execution graph, stages, model file identity or lineage',
                *stage_warnings_all]
    if any(reference.get('status') in ('unparsed', 'malformed') for reference in hash_references):
        warnings.append('One or more source-declared model hashes could not be fully parsed')
    if len(stages)>1:
        # Source order is all the text proves. Naming a "primary" pass would guess
        # at execution order, so we refuse to and say so out loud.
        warnings.append(
            'Parameter text contains %d ordered stages; source order is preserved but no '
            'primary stage is designated because text does not prove execution order'
            % len(stages))
    return {'parseVersion':PARAMETERS_VERSION,'coverage':'partial','steps':steps,
            'prompts':[{'stepId':step['nodeId'],'role':side,'text':value,'sourceNodes':[]}
                       for step in steps for side in ('positive','negative') for value in step[side]],
            'models':models,'outputs':[],
            'warnings':warnings,
            'hashReferences':hash_references,
            'platformFields':platform_fields,
            'platformFieldsByStage':stage_fields if len(stages)>1 else None,
            'stageCount':len(stages),
            'platformFamily':'a1111_family',
            'raw':{'format':'a1111_parameters','text':text,'fields':raw_fields,'source':source}}
