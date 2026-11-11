"""Reproducible instance generator for Appendix A. Python 3.10+, standard library.

Run: python generate_instances.py
     python generate_instances.py --config config.json --output generated --seed 123
The exported routes use the channel and area names in Appendix A.
"""
import argparse
import copy
import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from default_config import default_config

EPS = 1e-7

def choose(rng, values, weights):
    return rng.choices(values, weights=weights, k=1)[0]

def channel_minutes(channel):
    """Whole-minute sailing durations, truncated as in Appendix Table A1."""
    return math.floor(60*channel['length_nm']/channel['speed_kn'] + EPS)

def validate_config(c):
    assert 0 <= c['autonomous_share'] <= 1
    assert 0 < c['inbound_share'] < 1
    assert 0 < c['peak_daily_share'] < 1
    assert 0 <= c['previous_start'] < c['horizon_start'] < c['horizon_end'] <= 1440
    assert c['cases_per_density'] > 0
    bands = list(c['density_bands'].values())
    assert all(a <= b for a,b in bands)
    assert all(b < x or y < a for i,(a,b) in enumerate(bands) for x,y in bands[i+1:])
    assert all(v['mean_interarrival_h']>0 and all(s['weight']>0 and s['lower_t']<=s['upper_t'] for s in v['size_classes']) for v in c['vessels'])
    assert all(0<a['safety_factor']<=1 and math.floor(a['capacity']*a['safety_factor']+EPS)>=1 and a['occupancy_min']>0 for a in c['areas'])
    assert all(x['length_nm']>0 and x['speed_kn']>0 for x in c['channels'])
    assert all(0 < v < 1 for v in c['via4_channel_fraction'].values())
    assert all(float(c[k]).is_integer() for k in
               ('previous_start','horizon_start','horizon_end','safety_interval_min','berth_clearance_min'))
    assert c['safety_interval_min']>=1 and c['berth_clearance_min']>=1
    assert all(float(a['occupancy_min']).is_integer() for a in c['areas'])
    assert all(channel_minutes(ch)>=1 for ch in c['channels'])
    for name, fraction in c['via4_channel_fraction'].items():
        total=channel_minutes(next(ch for ch in c['channels'] if ch['channel']==name))
        assert 0 < math.floor(total*fraction + EPS) < total

def generate_arrivals(c, rng, start, end, factor):
    """Sample exponential arrivals, then assign each arrival to its integer minute bin."""
    tasks=[]
    for vessel in c['vessels']:
        for direction in ('in','out'):
            direction_factor = 1 if direction=='in' else (1-c['inbound_share'])/c['inbound_share']
            rate = factor*direction_factor/(vessel['mean_interarrival_h']*60)
            at=start+rng.expovariate(rate)
            while at < end:
                tasks.append(dict(vessel_type=vessel['vessel_type'],direction=direction,ready_min=math.floor(at)))
                at += rng.expovariate(rate)
    return sorted(tasks,key=lambda t:(t['ready_min'],t['vessel_type'],t['direction']))

def route_steps(c, route_id, direction):
    route=next(r for r in c['routes'] if r['route_id']==route_id)
    channels={x['channel']:x for x in c['channels']}
    areas={x['area']:x for x in c['areas']}
    tokens=route['steps'] if direction=='in' else list(reversed(route['steps']))
    rows=[]; at=0
    for token in tokens:
        if token in areas:
            kind='area'; name=token; duration=int(areas[token]['occupancy_min'])
        else:
            kind='channel'; name=token.split(':')[0]; ch=channels[name]
            duration=channel_minutes(ch)
            if ':' in token:
                f=c['via4_channel_fraction'][name]
                before=math.floor(duration*f + EPS)
                duration = before if token.endswith('before4') else duration-before
        rows.append(dict(sequence=len(rows)+1,kind=kind,resource=name,segment=token,
                         entry_offset_min=at,exit_offset_min=at+duration))
        at+=duration
    return rows

def enrich(c, rng, tasks, prefix):
    vessels={v['vessel_type']:v for v in c['vessels']}
    for i,t in enumerate(tasks,1):
        v=vessels[t['vessel_type']]
        sc=choose(rng,v['size_classes'],[s['weight'] for s in v['size_classes']])
        lower=sc['lower_t'] if sc is v['size_classes'][0] else sc['lower_t']+1
        ton=rng.randint(lower,sc['upper_t'])
        ports=[p for p,classes in c['berth_classes'].items() if t['vessel_type'] in classes and max(classes[t['vessel_type']])>=ton]
        if not ports: raise ValueError(f'No compatible berth for {t["vessel_type"]}, {ton} t')
        port=choose(rng,ports,[c['port_weights'][p] for p in ports])
        routes=[r for r in c['routes'] if r['port']==port]
        r=choose(rng,routes,[r['weight'] for r in routes])
        capacity=min(x for x in c['berth_classes'][port][t['vessel_type']] if x>=ton)
        t.update(task_id=f'{prefix}{i:03}',tonnage_t=ton,size_lower_t=sc['lower_t'],size_upper_t=sc['upper_t'],
                 port=port,route_id=r['route_id'],autonomous=0,lng=int(t['vessel_type']=='LNG carrier'),
                 berth_id=f'{prefix}-{port}-B{i:03}',berth_capacity_t=capacity,
                 predecessor='',weight=c['lng_delay_weight'] if t['vessel_type']=='LNG carrier' else c['non_lng_delay_weight'])
        t['steps']=route_steps(c,r['route_id'],t['direction'])
        t['travel_min']=t['steps'][-1]['exit_offset_min']
    eligible=[t for t in tasks if t['vessel_type'] in c['autonomous_eligible_types']]
    target=math.floor(len(tasks)*c['autonomous_share']+.5)
    if target>len(eligible): raise ValueError('Not enough eligible vessels for the requested autonomous share')
    for t in rng.sample(eligible,target):t['autonomous']=1

def pair_berths(c,rng,tasks):
    """Pair inbound and outbound tasks with compatible ports, cargo types and tonnages."""
    pairs=[(a,b) for i,a in enumerate(tasks) for b in tasks[i+1:]
           if a['port']==b['port'] and a['vessel_type']==b['vessel_type']
           and a['direction']!=b['direction'] and not a['lng'] and not b['lng']]
    rng.shuffle(pairs); used=set(); count=0
    for a,b in pairs:
        if count>=math.floor(len(tasks)*c['berth_pair_fraction']+.5):break
        if a['task_id'] in used or b['task_id'] in used:continue
        inc,out=(a,b) if a['direction']=='in' else (b,a)
        max_t=max(a['tonnage_t'],b['tonnage_t'])
        cap=min(x for x in c['berth_classes'][a['port']][a['vessel_type']] if x>=max_t)
        inc['berth_id']=out['berth_id']; inc['predecessor']=out['task_id']
        inc['berth_capacity_t']=out['berth_capacity_t']=cap
        used.update([a['task_id'],b['task_id']]);count+=1
    return count

def events(t,start):
    return [dict(task_id=t['task_id'],kind=s['kind'],resource=s['resource'],
                 enter=start+s['entry_offset_min'],exit=start+s['exit_offset_min'],
                 autonomous=t['autonomous'],lng=t['lng'],direction=t['direction'],
                 offset=s['entry_offset_min']) for s in t['steps']]

def conflict_shift(c,t,start,book):
    """Event sweep; half-open occupancy intervals; no sampling-grid approximation."""
    areas={a['area']:a for a in c['areas']}
    channels={a['channel']:a for a in c['channels']}
    shift=0
    for e in events(t,start):
        existing=book.get((e['kind'],e['resource']),[])
        if e['kind']=='channel':
            for o in existing:
                overlap=e['enter']<o['exit']-EPS and o['enter']<e['exit']-EPS
                opposed=channels[e['resource']]['one_way'] and e['direction']!=o['direction']
                if overlap and opposed:shift=max(shift,o['exit']-e['enter'])
                if e['direction']==o['direction'] and abs(e['enter']-o['enter']) < c['safety_interval_min']-EPS:
                    shift=max(shift,o['enter']+c['safety_interval_min']-e['enter'])
        else:
            overlapping=[o for o in existing if e['enter']<o['exit']-EPS and o['enter']<e['exit']-EPS]
            cuts=sorted({e['enter']}|{max(e['enter'],o['enter']) for o in overlapping})
            for at in cuts:
                active=[o for o in overlapping if o['enter']<=at+EPS/10 and o['exit']>at+EPS/10]
                if not active:continue
                mixed=e['autonomous'] or any(o['autonomous'] for o in active)
                area=areas[e['resource']]
                cap=math.floor(area['capacity']*(area['safety_factor'] if mixed else 1)+EPS)
                forbidden=(e['lng'] and any(o['autonomous'] for o in active)) or (e['autonomous'] and any(o['lng'] for o in active))
                if len(active)+1>cap or forbidden:
                    shift=max(shift,min(o['exit'] for o in active)-e['enter'])
    return math.ceil(shift)

def schedule(c,tasks,initial=None):
    book=defaultdict(list)
    for e in initial or []:book[e['kind'],e['resource']].append(e)
    starts={}; remaining=sorted(tasks,key=lambda t:(t['ready_min'],t['task_id']))
    while remaining:
        ready=[t for t in remaining if not t['predecessor'] or t['predecessor'] in starts]
        if not ready:raise ValueError('Cyclic berth precedence')
        t=ready[0]; start=math.ceil(t['ready_min'])
        if t['predecessor']:
            start=max(start,math.ceil(starts[t['predecessor']]+c['berth_clearance_min']-t['travel_min']))
        for _ in range(10000):
            shift=conflict_shift(c,t,start,book)
            if shift<=0:break
            start+=shift
        else:raise RuntimeError('Reference schedule did not converge')
        starts[t['task_id']]=start
        for e in events(t,start):book[e['kind'],e['resource']].append(e)
        remaining.remove(t)
    return starts,[e for group in book.values() for e in group]

def make_case(c,seed):
    rng=random.Random(seed)
    hours=(c['horizon_end']-c['horizon_start'])/60
    peak=c['peak_daily_share']/(hours/24)
    off=(1-c['peak_daily_share'])/(1-hours/24)
    current=generate_arrivals(c,rng,c['horizon_start'],c['horizon_end'],peak)
    group=next((k for k,(a,b) in c['density_bands'].items() if a<=len(current)<=b),None)
    if group is None:return None,'outside density ranges'
    previous=generate_arrivals(c,rng,c['previous_start'],c['horizon_start'],off)
    enrich(c,rng,previous,'P')
    prior_start,prior_events=schedule(c,previous)
    boundary=c['horizon_start']
    if any(prior_start[t['task_id']]>=boundary for t in previous):return None,'unstarted previous tasks'
    inherited=[t for t in previous if prior_start[t['task_id']]+t['travel_min']>boundary+EPS]
    if not c['inherited_count_range'][0]<=len(inherited)<=c['inherited_count_range'][1]:return None,'inherited count outside range'
    inherited_ids={t['task_id'] for t in inherited}
    fixed=[dict(e) for e in prior_events if e['task_id'] in inherited_ids and e['exit']>boundary+EPS]
    enrich(c,rng,current,'C')
    pair_count=pair_berths(c,rng,current)
    current_start,_=schedule(c,current,fixed)
    return dict(seed=seed,density=group,current=current,previous=previous,inherited=inherited,
                prior_start=prior_start,prior_events=prior_events,fixed=fixed,
                current_start=current_start,berth_pairs=pair_count),None

def validate_case(c,case):
    """Independent assertions on data and full reference schedules."""
    current=case['current']; lo,hi=c['density_bands'][case['density']]
    assert lo<=len(current)<=hi
    assert sum(t['autonomous'] for t in current)==math.floor(len(current)*c['autonomous_share']+.5)
    assert all(c['horizon_start']<=t['ready_min']<c['horizon_end'] for t in current)
    assert all(t['berth_capacity_t']>=t['tonnage_t'] for t in current+case['previous'])
    assert all(not(t['lng'] and t['autonomous']) for t in current+case['previous'])
    for tasks,starts,initial in [(case['previous'],case['prior_start'],[]),(current,case['current_start'],case['fixed'])]:
        all_events=initial+[e for t in tasks for e in events(t,starts[t['task_id']])]
        for area in c['areas']:
            es=[e for e in all_events if e['kind']=='area' and e['resource']==area['area']]
            cuts=sorted({e['enter'] for e in es}|{e['exit'] for e in es})
            for a,b in zip(cuts,cuts[1:]):
                if b-a<=1e-6:continue
                active=[e for e in es if e['enter']<=(a+b)/2<e['exit']]
                auto=any(e['autonomous'] for e in active)
                limit=math.floor(area['capacity']*(area['safety_factor'] if auto else 1)+EPS)
                assert len(active)<=limit,(case['seed'],area,active)
                assert not(auto and any(e['lng'] for e in active))
        for ch in c['channels']:
            es=[e for e in all_events if e['kind']=='channel' and e['resource']==ch['channel']]
            for i,a in enumerate(es):
                for b in es[i+1:]:
                    if a['task_id']==b['task_id']:continue
                    if a['direction']==b['direction']:
                        assert abs(a['enter']-b['enter'])>=c['safety_interval_min']-1e-6
                    if ch['one_way'] and a['direction']!=b['direction']:
                        assert min(a['exit'],b['exit'])-max(a['enter'],b['enter'])<=1e-6
        for t in tasks:
            assert starts[t['task_id']]>=t['ready_min']-EPS
            if t['predecessor']:
                assert starts[t['task_id']]+t['travel_min']>=starts[t['predecessor']]+c['berth_clearance_min']-EPS
    for t in case['inherited']:
        assert case['prior_start'][t['task_id']]<c['horizon_start']<case['prior_start'][t['task_id']]+t['travel_min']

def generate(c):
    validate_config(c)
    counts=Counter(); rejected=Counter(); cases=[]; attempts=[]
    for attempt in range(c['max_attempts']):
        seed=c['base_seed']+attempt
        case,reason=make_case(c,seed)
        if case and counts[case['density']]>=c['cases_per_density']:reason='density group full';case=None
        if case:
            validate_case(c,case)
            counts[case['density']]+=1
            case['instance']=case['density']+str(counts[case['density']]);cases.append(case)
            attempts.append(dict(seed=seed,result=case['instance']))
            print(f"{case['instance']}: current={len(case['current'])}, inherited={len(case['inherited'])}, seed={seed}")
        else:
            rejected[reason]+=1;attempts.append(dict(seed=seed,result=reason))
        if all(counts[g]==c['cases_per_density'] for g in c['density_bands']):break
    else:raise RuntimeError(f'Only generated {dict(counts)}. Increase max_attempts or review arrival/profile settings.')
    cases.sort(key=lambda x:(list(c['density_bands']).index(x['density']),x['instance']))
    return cases,attempts,rejected

def flatten_config(value,prefix=''):
    rows=[]
    if isinstance(value,dict):
        for k,v in value.items():rows.extend(flatten_config(v,f'{prefix}.{k}' if prefix else k))
    elif isinstance(value,list):
        rows.append({'setting':prefix,'value':json.dumps(value,ensure_ascii=False,separators=(',',':'))})
    else:rows.append({'setting':prefix,'value':value})
    return rows

def export_tables(c,cases,attempts):
    result={k:[] for k in ['Summary','Tasks','RouteSteps','FixedOccupations','PriorSchedule','Channels','Areas','Routes','BerthClasses','VesselTypes','Settings','GenerationLog']}
    for case in cases:
        name=case['instance'];tasks=case['current'];inherited=case['inherited'];ids={t['task_id'] for t in inherited}
        result['Summary'].append(dict(instance=name,density=case['density'],seed=case['seed'],
            current_tasks=len(tasks),inbound=sum(t['direction']=='in' for t in tasks),outbound=sum(t['direction']=='out' for t in tasks),
            autonomous=sum(t['autonomous'] for t in tasks),autonomous_share=sum(t['autonomous'] for t in tasks)/len(tasks),
            lng=sum(t['lng'] for t in tasks),inherited_tasks=len(inherited),berth_pairs=case['berth_pairs'],
            latest_reference_finish_min=max(case['current_start'][t['task_id']]+t['travel_min'] for t in tasks),validation='PASS'))
        for role,group in [('current',tasks),('inherited',inherited)]:
            for t in group:
                start=case['current_start'][t['task_id']] if role=='current' else case['prior_start'][t['task_id']]
                result['Tasks'].append(dict(instance=name,task_id=t['task_id'],role=role,direction=t['direction'],
                    vessel_type=t['vessel_type'],tonnage_t=t['tonnage_t'],autonomous=t['autonomous'],lng=t['lng'],
                    ready_min=t['ready_min'],port=t['port'],berth_id=t['berth_id'],berth_capacity_t=t['berth_capacity_t'],
                    route_id=t['route_id'],route=' > '.join(s['segment'] for s in t['steps']),
                    delay_weight=t['weight'],berth_predecessor=t['predecessor'],reference_start_min=start,
                    reference_finish_min=start+t['travel_min'],generation_seed=case['seed']))
                for s in t['steps']:result['RouteSteps'].append(dict(instance=name,task_id=t['task_id'],**s))
        for e in case['fixed']:
            result['FixedOccupations'].append(dict(instance=name,task_id=e['task_id'],kind=e['kind'],resource=e['resource'],
                original_entry_min=e['enter'],remaining_entry_min=max(e['enter'],c['horizon_start']),exit_min=e['exit'],
                remaining_duration_min=e['exit']-max(e['enter'],c['horizon_start']),autonomous=e['autonomous'],lng=e['lng'],direction=e['direction']))
        for t in case['previous']:
            start=case['prior_start'][t['task_id']]
            result['PriorSchedule'].append(dict(instance=name,task_id=t['task_id'],role='inherited' if t['task_id'] in ids else 'completed',
                direction=t['direction'],vessel_type=t['vessel_type'],tonnage_t=t['tonnage_t'],port=t['port'],berth_id=t['berth_id'],
                berth_capacity_t=t['berth_capacity_t'],route_id=t['route_id'],autonomous=t['autonomous'],lng=t['lng'],
                ready_min=t['ready_min'],start_min=start,finish_min=start+t['travel_min']))
    result['Channels']=[dict(**ch,sailing_time_min=channel_minutes(ch),source='Appendix A1') for ch in c['channels']]
    result['Areas']=[dict(**a,effective_capacity_with_autonomous=math.floor(a['capacity']*a['safety_factor']+EPS)) for a in c['areas']]
    for r in c['routes']:
        total=sum(q['weight'] for q in c['routes'] if q['port']==r['port'])
        result['Routes'].append(dict(port=r['port'],route_id=r['route_id'],legacy_route=r['legacy_route'],retained_weight=r['weight'],
            probability=r['weight']/total,inbound_route=' > '.join(r['steps']+[r['port']]),
            outbound_route=' > '.join([r['port']]+list(reversed(r['steps']))),source='Configured route weights'))
    for p,groups in c['berth_classes'].items():
        for typ,classes in groups.items():
            for cap in classes:result['BerthClasses'].append(dict(port=p,vessel_type=typ,berth_capacity_t=cap,port_weight=c['port_weights'][p]))
    for v in c['vessels']:
        total=sum(s['weight'] for s in v['size_classes'])
        for s in v['size_classes']:
            result['VesselTypes'].append(dict(vessel_type=v['vessel_type'],lower_t=s['lower_t'],upper_t=s['upper_t'],
                table_weight_pct=s['weight'],sampling_probability=s['weight']/total,mean_interarrival_h=v['mean_interarrival_h'],source='Appendix A3'))
    separately_tabulated={'channels','areas','routes','berth_classes','vessels'}
    result['Settings']=flatten_config({k:v for k,v in c.items() if k not in separately_tabulated})
    result['GenerationLog']=attempts
    return result

def task_sheet(tables):
    """Only scheduling inputs; current tasks must not inherit reference starts."""
    rows=[]
    for task in tables['Tasks']:
        row={k:v for k,v in task.items() if k not in ('reference_start_min','reference_finish_min','generation_seed')}
        row['fixed_start_min']=task['reference_start_min'] if task['role']=='inherited' else None
        row['generation_seed']=task['generation_seed']
        rows.append(row)
    return {'Tasks':rows}


def write_data(c,tables,out):
    out.mkdir(parents=True,exist_ok=True)
    (out/'config_used.json').write_text(json.dumps(c,indent=2,ensure_ascii=False),encoding='utf-8')
    (out/'tables.json').write_text(json.dumps(tables,indent=2,ensure_ascii=False),encoding='utf-8')
    exported=task_sheet(tables)
    (out/'tasks.json').write_text(json.dumps(exported,indent=2,ensure_ascii=False),encoding='utf-8')
    csvdir=out/'csv';csvdir.mkdir(exist_ok=True)
    for name,rows in exported.items():
        with (csvdir/f'{name}.csv').open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    digest=hashlib.sha256(json.dumps(tables,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    (out/'validation.json').write_text(json.dumps(dict(status='PASS',cases=len(tables['Summary']),data_sha256=digest,
        checks=['density bands','tonnage/berth compatibility','autonomous count and LNG eligibility',
                'prior snapshot consistency','reference area capacities','LNG/autonomous exclusion',
                'channel safety intervals','one-way channels','berth precedence']),indent=2),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path(__file__).with_name('config.json'))
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('generated'))
    parser.add_argument('--seed',type=int)
    parser.add_argument('--cases-per-density',type=int)
    parser.add_argument('--data-only',action='store_true',help='Write CSV/JSON only; omit this option to also export scheduling_instances.xlsx.')
    args=parser.parse_args()
    c=json.loads(args.config.read_text(encoding='utf-8')) if args.config.exists() else default_config()
    if args.seed is not None:c['base_seed']=args.seed
    if args.cases_per_density is not None:c['cases_per_density']=args.cases_per_density
    cases,attempts,rejected=generate(c)
    tables=export_tables(c,cases,attempts);write_data(c,tables,args.output)
    if not args.data_only:
        from xlsx_export import export_xlsx
        export_xlsx(task_sheet(tables),args.output/'scheduling_instances.xlsx')
    print('Rejected candidates:',dict(rejected))
    print('Output:',args.output.resolve())

if __name__=='__main__':main()
