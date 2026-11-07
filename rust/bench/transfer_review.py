"""Local native coordinator/provider A/B; never connects a SIP bridge or writes platform state.
Measured endpoint: last input silence candidate -> first local audio enqueue, not device audibility.
Private config/traces/answers remain under the requested mode-0700 output directory.
"""
import argparse, hashlib, json, math, os, statistics, subprocess
from pathlib import Path


def measure(folder):
    events = [json.loads(line) for line in (folder / 'trace.jsonl').read_text().splitlines()]
    summary = json.loads((folder / 'summary.json').read_text())
    enqueue = next(e for e in events if e['event'] == 'audio_first_enqueued' and e['turn'] > 0)
    turn, attempt = enqueue['turn'], enqueue['attempt']
    def first(name, predicate=lambda e: True):
        return next(e for e in events if e['event'] == name and e['turn'] == turn and predicate(e))
    commit = first('user_speech_end')
    silence = [e for e in events if e['event'] == 'user_audio_end_candidate' and e['turn'] == turn and e['at_us'] <= commit['at_us']][-1]
    final = first('stt_final')
    token = first('llm_first_token', lambda e: e['attempt'] == attempt and e['data'].get('purpose') != 'transfer_route')
    release = first('llm_text_released', lambda e: e['attempt'] == attempt)
    tts = first('tts_request_start', lambda e: e['attempt'] == attempt)
    pcm = first('tts_first_pcm', lambda e: e['attempt'] == attempt)
    usage = summary['usage']
    answer = (folder/'answer.txt').read_text()
    return dict(mode=summary['mode'], end_to_enqueue_ms=(enqueue['at_us']-silence['at_us'])/1000,
                vad_ms=(commit['at_us']-silence['at_us'])/1000, asr_ms=(final['at_us']-commit['at_us'])/1000,
                guard_hold_ms=(release['at_us']-token['at_us'])/1000, tts_first_pcm_ms=(pcm['at_us']-tts['at_us'])/1000,
                llm_requests=usage['llm_requests'], input_tokens=usage.get('llm_input_tokens'), output_tokens=usage.get('llm_output_tokens'),
                cached_tokens=usage.get('llm_cached_tokens'), token_measurement=usage['measurement_status']['llm_input_tokens'],
                turns=sum(e['event']=='user_speech_end' for e in events), yoruba_diacritics=any(ord(c)>127 for c in answer), answer_chars=len(answer))


def main():
    parser=argparse.ArgumentParser(__doc__)
    for name in ['binary','config','pcm','output']: parser.add_argument(name,type=Path)
    parser.add_argument('--blocks',type=int,default=2,help='ABBA blocks; two observations per variant per block')
    args=parser.parse_args()
    if not 1 <= args.blocks <= 10: parser.error('blocks must be between 1 and 10')
    os.umask(0o077);args.output.mkdir(parents=True,exist_ok=True)
    results=[]
    for i,mode in enumerate(['baseline','stream','stream','baseline']*args.blocks):
        folder=args.output/f'{i:02d}-{mode}'
        with (args.output/f'{i:02d}.log').open('w') as log:
            subprocess.run([str(args.binary.resolve()),str(args.config.resolve()),str(args.pcm.resolve()),str(folder.resolve()),mode],stdout=log,stderr=log,check=True,timeout=90)
        row=measure(folder);row['binary_sha256']=hashlib.sha256(args.binary.read_bytes()).hexdigest();results.append(row)
        (args.output/'measurements.json').write_text(json.dumps(results,indent=2))
        print(json.dumps(row),flush=True)
    report={}
    for mode in ['baseline','stream']:
        rows=[r for r in results if r['mode']==mode and r['turns']==1]
        values=sorted(r['end_to_enqueue_ms'] for r in rows)
        if not values: raise RuntimeError('no comparable single-turn observations')
        report[mode]={'n':len(rows),'p50_ms':statistics.median(values),'observed_nearest_rank_p95_ms':values[math.ceil(.95*len(values))-1],
                      **{f'mean_{key}':statistics.mean(r[key] for r in rows) for key in ['guard_hold_ms','asr_ms','tts_first_pcm_ms','llm_requests','input_tokens','output_tokens','cached_tokens']}}
    (args.output/'comparison.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
if __name__=='__main__': main()
