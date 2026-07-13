#!/usr/bin/env python3
"""Validate a single batch result file against the input batch and allowed values."""
import json, sys, os

ALLOWED = {
    'target_exists': {'yes','no','uncertain'},
    'outparty_target_exists': {'yes','no','uncertain'},
    'target_party': {'D','R','I','other','none','uncertain'},
    'formulaic_address': {'yes','no','uncertain'},
    'procedural_deference': {'yes','no','uncertain'},
    'gratitude_praise': {'yes','no','uncertain'},
    'bipartisan_cooperation': {'yes','no','uncertain'},
    'personal_attack': {'yes','no','uncertain'},
    'misconduct_allegation': {'yes','no','uncertain'},
    'ideological_label': {'yes','no','uncertain'},
    'profanity': {'yes','no','uncertain'},
    'identity_slur': {'yes','no','uncertain'},
    'quoted_or_read_in': {'yes','no','uncertain'},
    'ambiguous': {'yes','no'},
    'confidence': {'low','medium','high'},
}
CATS = [k for k in ALLOWED]

def main(bi):
    inp = f'_adj_work/input/batch_{bi:02d}.json'
    res = f'_adj_work/results/batch_{bi:02d}.json'
    if not os.path.exists(res):
        print(f'MISSING result file: {res}'); return 1
    with open(inp, encoding='utf-8') as f: indata = json.load(f)
    with open(res, encoding='utf-8') as f: rdata = json.load(f)
    in_ids = [r['sample_id'] for r in indata]
    rmap = {r['sample_id']: r for r in rdata}
    errs = []
    for sid in in_ids:
        if sid not in rmap:
            errs.append(f'{sid}: missing from results'); continue
        r = rmap[sid]
        for fld, allowed in ALLOWED.items():
            v = r.get(fld, None)
            if v is None or str(v).strip()=='':
                errs.append(f'{sid}: {fld} blank')
            elif v not in allowed:
                errs.append(f'{sid}: {fld}={v!r} not allowed')
        rat = r.get('rationale','')
        if not rat or not str(rat).strip():
            errs.append(f'{sid}: rationale blank')
    extra = set(rmap) - set(in_ids)
    if extra: errs.append(f'extra sample_ids: {extra}')
    if errs:
        print(f'batch_{bi:02d}: {len(errs)} ERRORS')
        for e in errs[:50]: print('  ', e)
        return 1
    print(f'batch_{bi:02d}: OK ({len(in_ids)} rows)')
    return 0

if __name__=='__main__':
    sys.exit(main(int(sys.argv[1])))
