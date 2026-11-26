import math
from collections import defaultdict
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from xml.etree.ElementTree import Element, SubElement, tostring

EPS = 1e-7


def channel_minutes(channel):
    if 'sailing_time_min' in channel:
        value = channel['sailing_time_min']
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError('sailing_time_min must be a positive integer')
        return value
    return math.floor(60*channel['length_nm']/channel['speed_kn'] + EPS)


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


def autonomous_count(c, rng, count):
    expected = count * c['autonomous_share']
    lower = math.floor(expected)
    method = c.get('autonomous_count_method', 'nearest')
    if method == 'floor':
        return lower
    if method == 'stochastic':
        remainder = expected - lower
        return lower + int(remainder > 0 and rng.random() < remainder)
    return math.floor(expected + 0.5)


def events(t,start):
    return [dict(task_id=t['task_id'],kind=s['kind'],resource=s['resource'],
                 enter=start+s['entry_offset_min'],exit=start+s['exit_offset_min'],
                 autonomous=t['autonomous'],lng=t['lng'],direction=t['direction'],
                 offset=s['entry_offset_min']) for s in t['steps']]


def opposite_direction_restricted(c, channel, first, second):
    return (channel['one_way'] and first['direction'] != second['direction']
            and (not c.get('one_way_requires_lng', False) or first['lng'] or second['lng']))


def conflict_shift(c,t,start,book):
    areas={a['area']:a for a in c['areas']}
    channels={a['channel']:a for a in c['channels']}
    shift=0
    for e in events(t,start):
        existing=book.get((e['kind'],e['resource']),[])
        if e['kind']=='channel':
            for o in existing:
                overlap=e['enter']<o['exit']-EPS and o['enter']<e['exit']-EPS
                opposed=opposite_direction_restricted(c, channels[e['resource']], e, o)
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
    # Water depth in this port area is sufficient for all vessels to navigate at any time of day.
    if tuple(c.get('daily_tidal_window_min', (0, 1440))) != (0, 1440):
        raise ValueError('This reference scheduler supports only a full-day tidal window [0, 1440).')
    book=defaultdict(list)
    for e in initial or []:book[e['kind'],e['resource']].append(e)
    starts={}; remaining=sorted(tasks,key=lambda t:(t['ready_min'],t['task_id']))
    while remaining:
        ready=[t for t in remaining if not t['predecessor'] or t['predecessor'] in starts]
        if not ready:raise ValueError('Cyclic berth precedence')
        t=ready[0]; start=math.ceil(t['ready_min'])
        if t['predecessor']:
            earliest_arrival = starts[t['predecessor']] + c['berth_clearance_min']
            berth_travel = t['travel_min'] + c.get('berth_clearance_offset_min', 0)
            start = max(start, math.ceil(earliest_arrival - berth_travel))
        for _ in range(10000):
            shift=conflict_shift(c,t,start,book)
            if shift<=0:break
            start+=shift
        else:raise RuntimeError('Reference schedule did not converge')
        starts[t['task_id']]=start
        for e in events(t,start):book[e['kind'],e['resource']].append(e)
        remaining.remove(t)
    return starts,[e for group in book.values() for e in group]


def validate_case(c,case):
    current=case['current']; lo,hi=c['density_bands'][case['density']]
    assert lo<=len(current)<=hi
    target = len(current)*c['autonomous_share']
    mode = c.get('autonomous_count_method', 'nearest')
    n_auto = sum(t['autonomous'] for t in current)
    if mode == 'stochastic':
        assert math.floor(target) <= n_auto <= math.ceil(target)
    else:
        assert n_auto == (math.floor(target) if mode == 'floor' else math.floor(target+.5))
    current_ids = {t['task_id'] for t in current}
    inherited_ids = {t['task_id'] for t in case['inherited']}
    assert len(current_ids) == len(current) and current_ids.isdisjoint(inherited_ids)
    for t in case['previous']:
        assert (t['task_id'] in current_ids) == (case['prior_start'][t['task_id']] >= c['horizon_start'])
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
                assert len(active)<=limit,(area,active)
                assert not(auto and any(e['lng'] for e in active))
        for ch in c['channels']:
            es=[e for e in all_events if e['kind']=='channel' and e['resource']==ch['channel']]
            for i,a in enumerate(es):
                for b in es[i+1:]:
                    if a['task_id']==b['task_id']:continue
                    if a['direction']==b['direction']:
                        assert abs(a['enter']-b['enter'])>=c['safety_interval_min']-1e-6
                    if opposite_direction_restricted(c, ch, a, b):
                        assert min(a['exit'],b['exit'])-max(a['enter'],b['enter'])<=1e-6
        for t in tasks:
            assert starts[t['task_id']]>=t['ready_min']-EPS
            if t['predecessor']:
                berth_arrival = starts[t['task_id']] + t['travel_min'] + c.get('berth_clearance_offset_min', 0)
                assert berth_arrival >= starts[t['predecessor']] + c['berth_clearance_min'] - EPS
    for t in case['inherited']:
        assert case['prior_start'][t['task_id']]<c['horizon_start']<case['prior_start'][t['task_id']]+t['travel_min']


NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'

def column(n):
    out=''
    while n:
        n,r=divmod(n-1,26);out=chr(65+r)+out
    return out

def xml(root):
    return tostring(root,encoding='utf-8',xml_declaration=True)

def write_part(archive, name, content):
    member = ZipInfo(name)
    member.compress_type = ZIP_DEFLATED
    archive.writestr(member, content)

def export_xlsx(tables,path):
    content=Element('Types',xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    for ext,typ in [('rels','application/vnd.openxmlformats-package.relationships+xml'),('xml','application/xml')]:
        SubElement(content,'Default',Extension=ext,ContentType=typ)
    for part,typ in [('/xl/workbook.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'),('/xl/styles.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')]:
        SubElement(content,'Override',PartName=part,ContentType=typ)
    rootrels=Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    SubElement(rootrels,'Relationship',Id='rId1',Type=REL+'/officeDocument',Target='xl/workbook.xml')
    wb=Element('workbook',xmlns=NS,attrib={'xmlns:r':REL});sheets=SubElement(wb,'sheets')
    rels=Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    SubElement(rels,'Relationship',Id='rIdStyles',Type=REL+'/styles',Target='styles.xml')
    styles=Element('styleSheet',xmlns=NS)
    fonts=SubElement(styles,'fonts',count='2')
    for bold in (False,True):
        font=SubElement(fonts,'font');SubElement(font,'sz',val='11');SubElement(font,'name',val='Arial')
        if bold:SubElement(font,'b')
    fills=SubElement(styles,'fills',count='2')
    SubElement(SubElement(fills,'fill'),'patternFill',patternType='none')
    SubElement(SubElement(fills,'fill'),'patternFill',patternType='gray125')
    borders=SubElement(styles,'borders',count='1');SubElement(borders,'border')
    base=SubElement(styles,'cellStyleXfs',count='1');SubElement(base,'xf',numFmtId='0',fontId='0',fillId='0',borderId='0')
    xfs=SubElement(styles,'cellXfs',count='2')
    for i in (0,1):
        xf=SubElement(xfs,'xf',numFmtId='0',fontId=str(i),fillId='0',borderId='0',xfId='0',applyAlignment='1')
        SubElement(xf,'alignment',vertical='center',wrapText='1')
    names=SubElement(styles,'cellStyles',count='1');SubElement(names,'cellStyle',name='Normal',xfId='0',builtinId='0')
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for idx,(name,records) in enumerate(tables.items(),1):
            headers=list(records[0]);matrix=[headers]+[[r[h] for h in headers] for r in records]
            SubElement(sheets,'sheet',name=name,sheetId=str(idx),attrib={'r:id':f'rId{idx}'})
            SubElement(rels,'Relationship',Id=f'rId{idx}',Type=REL+'/worksheet',Target=f'worksheets/sheet{idx}.xml')
            SubElement(content,'Override',PartName=f'/xl/worksheets/sheet{idx}.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            ws=Element('worksheet',xmlns=NS)
            end=f'{column(len(headers))}{len(matrix)}';SubElement(ws,'dimension',ref=f'A1:{end}')
            views=SubElement(ws,'sheetViews');view=SubElement(views,'sheetView',workbookViewId='0',showGridLines='0')
            SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
            SubElement(ws,'sheetFormatPr',defaultRowHeight='25')
            cols=SubElement(ws,'cols')
            for j,h in enumerate(headers,1):
                width=70 if h in ('value','inbound_route','outbound_route','route','source') else min(30,max(16,len(h)+2))
                SubElement(cols,'col',min=str(j),max=str(j),width=str(width),customWidth='1')
            data=SubElement(ws,'sheetData')
            for i,row in enumerate(matrix,1):
                maxlen=max(len(str(v)) for v in row)
                height=40 if i==1 else max(25,min(110,16*((maxlen//65)+1)))
                rr=SubElement(data,'row',r=str(i),ht=str(height),customHeight='1')
                for j,v in enumerate(row,1):
                    if v is None:continue
                    cc=SubElement(rr,'c',r=f'{column(j)}{i}',s='1' if i==1 else '0')
                    if isinstance(v,bool):cc.set('t','b');SubElement(cc,'v').text='1' if v else '0'
                    elif isinstance(v,(int,float)):SubElement(cc,'v').text=str(v)
                    else:
                        cc.set('t','inlineStr');SubElement(SubElement(cc,'is'),'t').text=str(v)
            SubElement(ws,'autoFilter',ref=f'A1:{end}')
            write_part(z,f'xl/worksheets/sheet{idx}.xml',xml(ws))
        write_part(z,'[Content_Types].xml',xml(content));write_part(z,'_rels/.rels',xml(rootrels))
        write_part(z,'xl/workbook.xml',xml(wb));write_part(z,'xl/_rels/workbook.xml.rels',xml(rels));write_part(z,'xl/styles.xml',xml(styles))


def load_network():
    return {
        "horizon_start": 360,
        "horizon_end": 720,
        "daily_tidal_window_min": [0, 1440],
        "density_bands": {
            "L": [
                20,
                25
            ],
            "M": [
                28,
                35
            ],
            "H": [
                36,
                45
            ]
        },
        "inherited_count_range": [
            2,
            5
        ],
        "autonomous_share": 0.3,
        "autonomous_count_method": "stochastic",
        "berth_preference_exponent": 1.0,
        "berth_clearance_min": 1,
        "berth_clearance_offset_min": 20,
        "safety_interval_min": 5,
        "one_way_requires_lng": True,
        "channels": [
            {
                "channel": "AB",
                "speed_kn": 10,
                "sailing_time_min": 40,
                "one_way": True
            },
            {
                "channel": "BC",
                "speed_kn": 13,
                "sailing_time_min": 25,
                "one_way": False
            },
            {
                "channel": "CD",
                "speed_kn": 9,
                "sailing_time_min": 15,
                "one_way": False
            },
            {
                "channel": "BE",
                "speed_kn": 7,
                "sailing_time_min": 40,
                "one_way": True
            },
            {
                "channel": "BF",
                "speed_kn": 7,
                "sailing_time_min": 30,
                "one_way": True
            },
            {
                "channel": "BG",
                "speed_kn": 7,
                "sailing_time_min": 25,
                "one_way": True
            },
            {
                "channel": "HD",
                "speed_kn": 7,
                "sailing_time_min": 30,
                "one_way": False
            },
            {
                "channel": "BH",
                "speed_kn": 7,
                "sailing_time_min": 30,
                "one_way": False
            },
            {
                "channel": "BJ",
                "speed_kn": 7,
                "sailing_time_min": 40,
                "one_way": True
            },
            {
                "channel": "DK",
                "speed_kn": 9,
                "sailing_time_min": 10,
                "one_way": False
            },
            {
                "channel": "DL",
                "speed_kn": 9,
                "sailing_time_min": 15,
                "one_way": False
            }
        ],
        "areas": [
            {
                "area": "1",
                "capacity": 2,
                "safety_factor": 1,
                "occupancy_min": 15
            },
            {
                "area": "2",
                "capacity": 1,
                "safety_factor": 1,
                "occupancy_min": 5
            },
            {
                "area": "3",
                "capacity": 1,
                "safety_factor": 1,
                "occupancy_min": 10
            },
            {
                "area": "4",
                "capacity": 1,
                "safety_factor": 1,
                "occupancy_min": 10
            },
            {
                "area": "5",
                "capacity": 6,
                "safety_factor": 0.9,
                "occupancy_min": 20
            }
        ],
        "via4_channel_fraction": {
            "BJ": 0.625,
            "BE": 0.625
        },
        "berth_classes": {
            "P1": {
                "LNG carrier": [
                    10000
                ],
                "Container ship": [
                    150000
                ],
                "General cargo ship": [
                    150000
                ],
                "Dry bulk carrier": [
                    150000
                ],
                "Product tanker": [
                    150000
                ]
            },
            "P2": {
                "LNG carrier": [
                    150000
                ],
                "Container ship": [
                    150000
                ],
                "General cargo ship": [
                    150000
                ],
                "Dry bulk carrier": [
                    150000
                ],
                "Product tanker": [
                    150000
                ]
            },
            "P3": {
                "LNG carrier": [
                    150000
                ],
                "Dry bulk carrier": [
                    2000,
                    5000,
                    150000
                ],
                "General cargo ship": [
                    2000,
                    5000,
                    150000
                ],
                "Container ship": [
                    150000
                ],
                "Product tanker": [
                    150000
                ]
            },
            "P4": {
                "Product tanker": [
                    1000,
                    5000,
                    10000,
                    50000
                ],
                "LNG carrier": [
                    50000
                ],
                "Container ship": [
                    150000
                ],
                "General cargo ship": [
                    150000
                ],
                "Dry bulk carrier": [
                    150000
                ]
            },
            "P5": {
                "Container ship": [
                    2000,
                    10000,
                    50000,
                    100000,
                    150000
                ],
                "General cargo ship": [
                    2000,
                    5000,
                    10000
                ]
            },
            "P6": {
                "Container ship": [
                    2000,
                    10000,
                    50000,
                    100000,
                    150000
                ],
                "General cargo ship": [
                    2000,
                    5000,
                    10000
                ]
            }
        },
        "berth_min_t": {
            "P1": {
                "LNG carrier": 1000,
                "Container ship": 1000,
                "General cargo ship": 1000,
                "Dry bulk carrier": 1000,
                "Product tanker": 1000
            },
            "P2": {
                "LNG carrier": 1000,
                "Container ship": 1000,
                "General cargo ship": 1000,
                "Dry bulk carrier": 1000,
                "Product tanker": 1000
            },
            "P3": {
                "LNG carrier": 1000,
                "Dry bulk carrier": 1000,
                "General cargo ship": 1000,
                "Container ship": 1000,
                "Product tanker": 1000
            },
            "P4": {
                "Product tanker": 1,
                "LNG carrier": 1000,
                "Container ship": 1000,
                "General cargo ship": 1000,
                "Dry bulk carrier": 1000
            },
            "P5": {
                "Container ship": 1000,
                "General cargo ship": 1000
            },
            "P6": {
                "Container ship": 1000,
                "General cargo ship": 1000
            }
        },
        "routes": [
            {
                "port": "P1",
                "route_id": "P1-R1",
                "legacy_route": 1,
                "weight": 0.6,
                "steps": [
                    "AB",
                    "5",
                    "BG"
                ]
            },
            {
                "port": "P1",
                "route_id": "P1-R2",
                "legacy_route": 2,
                "weight": 0.4,
                "steps": [
                    "2",
                    "BH",
                    "5",
                    "BG"
                ]
            },
            {
                "port": "P2",
                "route_id": "P2-R1",
                "legacy_route": 1,
                "weight": 0.7,
                "steps": [
                    "AB",
                    "5",
                    "BF"
                ]
            },
            {
                "port": "P2",
                "route_id": "P2-R2",
                "legacy_route": 2,
                "weight": 0.3,
                "steps": [
                    "2",
                    "BH",
                    "5",
                    "BF"
                ]
            },
            {
                "port": "P3",
                "route_id": "P3-R1",
                "legacy_route": 1,
                "weight": 0.5,
                "steps": [
                    "AB",
                    "5",
                    "BJ:before4",
                    "4",
                    "BJ:after4"
                ]
            },
            {
                "port": "P3",
                "route_id": "P3-R2",
                "legacy_route": 2,
                "weight": 0.25,
                "steps": [
                    "2",
                    "BH",
                    "5",
                    "BJ:before4",
                    "4",
                    "BJ:after4"
                ]
            },
            {
                "port": "P4",
                "route_id": "P4-R1",
                "legacy_route": 1,
                "weight": 0.5,
                "steps": [
                    "AB",
                    "5",
                    "BE:before4",
                    "4",
                    "BE:after4"
                ]
            },
            {
                "port": "P4",
                "route_id": "P4-R2",
                "legacy_route": 2,
                "weight": 0.25,
                "steps": [
                    "2",
                    "BH",
                    "5",
                    "BE:before4",
                    "4",
                    "BE:after4"
                ]
            },
            {
                "port": "P5",
                "route_id": "P5-R1",
                "legacy_route": 1,
                "weight": 0.35,
                "steps": [
                    "AB",
                    "5",
                    "BC",
                    "3",
                    "CD",
                    "1",
                    "DK"
                ]
            },
            {
                "port": "P5",
                "route_id": "P5-R2",
                "legacy_route": 4,
                "weight": 0.35,
                "steps": [
                    "2",
                    "BH",
                    "5",
                    "BC",
                    "3",
                    "CD",
                    "1",
                    "DK"
                ]
            },
            {
                "port": "P5",
                "route_id": "P5-R3",
                "legacy_route": 3,
                "weight": 0.1,
                "steps": [
                    "2",
                    "HD",
                    "1",
                    "DK"
                ]
            },
            {
                "port": "P6",
                "route_id": "P6-R1",
                "legacy_route": 1,
                "weight": 0.35,
                "steps": [
                    "AB",
                    "5",
                    "BC",
                    "3",
                    "CD",
                    "1",
                    "DL"
                ]
            },
            {
                "port": "P6",
                "route_id": "P6-R2",
                "legacy_route": 4,
                "weight": 0.35,
                "steps": [
                    "2",
                    "BH",
                    "5",
                    "BC",
                    "3",
                    "CD",
                    "1",
                    "DL"
                ]
            },
            {
                "port": "P6",
                "route_id": "P6-R3",
                "legacy_route": 3,
                "weight": 0.1,
                "steps": [
                    "2",
                    "HD",
                    "1",
                    "DL"
                ]
            }
        ],
        "vessels": [
            {
                "name": "Container ship",
                "minimum": 1000,
                "maximum": 150000,
                "mean_interarrival_h": 0.7879
            },
            {
                "name": "General cargo ship",
                "minimum": 1000,
                "maximum": 10000,
                "mean_interarrival_h": 3.5177
            },
            {
                "name": "Dry bulk carrier",
                "minimum": 1000,
                "maximum": 5000,
                "mean_interarrival_h": 16.3621
            },
            {
                "name": "Product tanker",
                "minimum": 1,
                "maximum": 50000,
                "mean_interarrival_h": 7.6213
            },
            {
                "name": "LNG carrier",
                "minimum": 1000,
                "maximum": 150000,
                "mean_interarrival_h": 7.7376
            }
        ],
        "traffic": {
            "inbound_share": 0.4723404255319149
        }
    }


def load_sampling_parameters():
    return {
        "weight_unit": "percent",
        "current_lng_counts": {
            "2": 50,
            "3": 50
        },
        "groups": {
            "L": {
                "roles": {
                    "ordinary": {
                        "tonnage_given_type": {
                            "Container ship": {
                                "10000": 20.80006606,
                                "5000": 18.33092224,
                                "100000": 4.53467055,
                                "20000": 24.93981224,
                                "150000": 6.0462274,
                                "3000": 9.0191455,
                                "50000": 8.87973676,
                                "1000": 7.44941925
                            },
                            "General cargo ship": {
                                "10000": 37.41049149,
                                "5000": 32.96954963,
                                "3000": 16.22161511,
                                "1000": 13.39834377
                            },
                            "Product tanker": {
                                "10000": 24.79530285,
                                "5000": 19.281013,
                                "20000": 21.9402822,
                                "3000": 10.51691531,
                                "50000": 16.45520977,
                                "1000": 7.01127687
                            },
                            "Dry bulk carrier": {
                                "5000": 52.38095445,
                                "3000": 28.57142733,
                                "1000": 19.04761822
                            }
                        },
                        "port_given_type_and_tonnage": {
                            "Container ship": {
                                "10000": {
                                    "3": 17.79263674,
                                    "5": 20.72093291,
                                    "6": 25.90116496,
                                    "1": 13.34447452,
                                    "4": 8.89631635,
                                    "2": 13.34447452
                                },
                                "5000": {
                                    "2": 25.68981199,
                                    "5": 29.39000752,
                                    "6": 23.51200708,
                                    "4": 12.84490405,
                                    "3": 8.56326936
                                },
                                "100000": {
                                    "1": 33.33333334,
                                    "4": 33.33333333,
                                    "5": 33.33333333
                                },
                                "20000": {
                                    "5": 36.36491881,
                                    "3": 19.69590231,
                                    "6": 24.24328104,
                                    "1": 4.92397446,
                                    "2": 14.77192338
                                },
                                "150000": {
                                    "5": 50.0,
                                    "3": 25.0,
                                    "6": 25.0
                                },
                                "3000": {
                                    "6": 23.89343178,
                                    "3": 26.10656822,
                                    "4": 17.40437881,
                                    "5": 23.89343178,
                                    "2": 8.70218941
                                },
                                "50000": {
                                    "1": 41.48872939,
                                    "2": 13.82957646,
                                    "4": 27.65915293,
                                    "5": 17.02254122
                                },
                                "1000": {
                                    "6": 28.92820641,
                                    "5": 28.92820641,
                                    "4": 42.14358718
                                }
                            },
                            "General cargo ship": {
                                "10000": {
                                    "3": 17.79263674,
                                    "5": 20.72093291,
                                    "6": 25.90116496,
                                    "1": 13.34447452,
                                    "4": 8.89631635,
                                    "2": 13.34447452
                                },
                                "5000": {
                                    "2": 25.68981199,
                                    "5": 29.39000752,
                                    "6": 23.51200708,
                                    "4": 12.84490405,
                                    "3": 8.56326936
                                },
                                "3000": {
                                    "6": 23.89343178,
                                    "3": 26.10656822,
                                    "4": 17.40437881,
                                    "5": 23.89343178,
                                    "2": 8.70218941
                                },
                                "1000": {
                                    "6": 28.92820641,
                                    "5": 28.92820641,
                                    "4": 42.14358718
                                }
                            },
                            "Product tanker": {
                                "10000": {
                                    "3": 33.3333384,
                                    "1": 24.9999981,
                                    "4": 16.6666654,
                                    "2": 24.9999981
                                },
                                "5000": {
                                    "2": 54.5454583,
                                    "4": 27.27272502,
                                    "3": 18.18181668
                                },
                                "20000": {
                                    "3": 50.00000569,
                                    "1": 12.49999858,
                                    "2": 37.49999573
                                },
                                "3000": {
                                    "3": 50.0,
                                    "4": 33.33333333,
                                    "2": 16.66666667
                                },
                                "50000": {
                                    "1": 50.0,
                                    "2": 16.66666667,
                                    "4": 33.33333333
                                },
                                "1000": {
                                    "4": 100.0
                                }
                            },
                            "Dry bulk carrier": {
                                "5000": {
                                    "2": 54.5454583,
                                    "4": 27.27272502,
                                    "3": 18.18181668
                                },
                                "3000": {
                                    "3": 50.0,
                                    "4": 33.33333333,
                                    "2": 16.66666667
                                },
                                "1000": {
                                    "4": 100.0
                                }
                            }
                        }
                    },
                    "lng": {
                        "tonnage_given_type": {
                            "LNG carrier": {
                                "50000": 6.666667,
                                "20000": 20.0,
                                "1000": 19.999999,
                                "10000": 13.333334,
                                "5000": 33.333333,
                                "150000": 6.666667
                            }
                        },
                        "port_given_type_and_tonnage": {
                            "LNG carrier": {
                                "50000": {
                                    "4": 100.0
                                },
                                "20000": {
                                    "3": 66.666665,
                                    "2": 33.333335
                                },
                                "1000": {
                                    "1": 66.666668,
                                    "4": 33.333332
                                },
                                "10000": {
                                    "4": 50.0,
                                    "1": 50.0
                                },
                                "5000": {
                                    "3": 40.0,
                                    "4": 20.000001,
                                    "2": 39.999999
                                },
                                "150000": {
                                    "3": 100.0
                                }
                            }
                        }
                    }
                }
            },
            "M": {
                "roles": {
                    "lng": {
                        "tonnage_given_type": {
                            "LNG carrier": {
                                "5000": 33.333334,
                                "150000": 6.666667,
                                "3000": 20.0,
                                "20000": 13.333334,
                                "50000": 6.666667,
                                "1000": 13.333332,
                                "10000": 6.666666
                            }
                        },
                        "port_given_type_and_tonnage": {
                            "LNG carrier": {
                                "5000": {
                                    "4": 20.000001,
                                    "3": 39.999998,
                                    "1": 20.000001,
                                    "2": 20.0
                                },
                                "150000": {
                                    "2": 100.0
                                },
                                "3000": {
                                    "4": 33.333335,
                                    "3": 66.666665
                                },
                                "20000": {
                                    "3": 50.0,
                                    "4": 50.0
                                },
                                "50000": {
                                    "3": 100.0
                                },
                                "1000": {
                                    "2": 50.0,
                                    "3": 50.0
                                },
                                "10000": {
                                    "4": 100.0
                                }
                            }
                        }
                    },
                    "ordinary": {
                        "tonnage_given_type": {
                            "Container ship": {
                                "150000": 5.614355,
                                "1000": 7.76697095,
                                "5000": 28.13234855,
                                "10000": 18.72035771,
                                "20000": 12.58287363,
                                "50000": 13.96998695,
                                "100000": 5.61435362,
                                "3000": 7.59875359
                            },
                            "General cargo ship": {
                                "1000": 12.48339254,
                                "5000": 45.21545817,
                                "10000": 30.08812255,
                                "3000": 12.21302674
                            },
                            "Dry bulk carrier": {
                                "5000": 62.50000321,
                                "1000": 17.50000046,
                                "3000": 19.99999633
                            },
                            "Product tanker": {
                                "5000": 26.21083314,
                                "1000": 7.3390331,
                                "20000": 14.05489202,
                                "10000": 25.26791956,
                                "50000": 18.73985755,
                                "3000": 8.38746463
                            }
                        },
                        "port_given_type_and_tonnage": {
                            "Container ship": {
                                "150000": {
                                    "3": 50.00001226,
                                    "6": 16.66666258,
                                    "1": 16.66666258,
                                    "2": 16.66666258
                                },
                                "1000": {
                                    "6": 26.5755112,
                                    "2": 26.77084856,
                                    "5": 26.5755112,
                                    "4": 13.38541936,
                                    "3": 6.69270968
                                },
                                "5000": {
                                    "4": 20.32546176,
                                    "3": 11.08661674,
                                    "5": 29.34859824,
                                    "1": 7.39107873,
                                    "6": 24.4571658,
                                    "2": 7.39107873
                                },
                                "10000": {
                                    "6": 22.05206517,
                                    "1": 15.93378823,
                                    "2": 6.37351342,
                                    "3": 28.68081507,
                                    "4": 15.93378823,
                                    "5": 11.02602988
                                },
                                "20000": {
                                    "3": 6.15344113,
                                    "4": 6.15344113,
                                    "5": 37.18251915,
                                    "6": 7.43650164,
                                    "2": 24.61377356,
                                    "1": 18.46032339
                                },
                                "50000": {
                                    "2": 22.16981329,
                                    "5": 20.09434098,
                                    "3": 16.62735385,
                                    "4": 22.16981328,
                                    "6": 13.39622732,
                                    "1": 5.54245128
                                },
                                "100000": {
                                    "5": 33.33333332,
                                    "6": 16.66666667,
                                    "2": 16.66666667,
                                    "1": 16.66666667,
                                    "4": 16.66666667
                                },
                                "3000": {
                                    "4": 20.52260849,
                                    "2": 13.68173899,
                                    "5": 27.16382642,
                                    "3": 13.68173899,
                                    "6": 18.10921761,
                                    "1": 6.8408695
                                }
                            },
                            "General cargo ship": {
                                "1000": {
                                    "6": 26.5755112,
                                    "2": 26.77084856,
                                    "5": 26.5755112,
                                    "4": 13.38541936,
                                    "3": 6.69270968
                                },
                                "5000": {
                                    "4": 20.32546176,
                                    "3": 11.08661674,
                                    "5": 29.34859824,
                                    "1": 7.39107873,
                                    "6": 24.4571658,
                                    "2": 7.39107873
                                },
                                "10000": {
                                    "6": 22.05206517,
                                    "1": 15.93378823,
                                    "2": 6.37351342,
                                    "3": 28.68081507,
                                    "4": 15.93378823,
                                    "5": 11.02602988
                                },
                                "3000": {
                                    "4": 20.52260849,
                                    "2": 13.68173899,
                                    "5": 27.16382642,
                                    "3": 13.68173899,
                                    "6": 18.10921761,
                                    "1": 6.8408695
                                }
                            },
                            "Dry bulk carrier": {
                                "5000": {
                                    "4": 43.99999552,
                                    "3": 24.00000024,
                                    "1": 16.00000212,
                                    "2": 16.00000212
                                },
                                "1000": {
                                    "2": 57.14286614,
                                    "4": 28.57142257,
                                    "3": 14.28571129
                                },
                                "3000": {
                                    "4": 37.5,
                                    "2": 25.0,
                                    "3": 25.0,
                                    "1": 12.5
                                }
                            },
                            "Product tanker": {
                                "5000": {
                                    "4": 43.99999552,
                                    "3": 24.00000024,
                                    "1": 16.00000212,
                                    "2": 16.00000212
                                },
                                "1000": {
                                    "2": 57.14286614,
                                    "4": 28.57142257,
                                    "3": 14.28571129
                                },
                                "20000": {
                                    "3": 11.1111093,
                                    "4": 11.1111093,
                                    "2": 44.44445351,
                                    "1": 33.33332789
                                },
                                "10000": {
                                    "1": 23.80952581,
                                    "2": 9.52380752,
                                    "3": 42.85714086,
                                    "4": 23.80952581
                                },
                                "50000": {
                                    "2": 33.33333741,
                                    "3": 24.99999388,
                                    "4": 33.33333742,
                                    "1": 8.33333129
                                },
                                "3000": {
                                    "4": 37.5,
                                    "2": 25.0,
                                    "3": 25.0,
                                    "1": 12.5
                                }
                            }
                        }
                    }
                }
            },
            "H": {
                "roles": {
                    "lng": {
                        "tonnage_given_type": {
                            "LNG carrier": {
                                "3000": 26.666667,
                                "1000": 20.0,
                                "5000": 26.666666,
                                "20000": 13.333334,
                                "150000": 6.666667,
                                "10000": 6.666666
                            }
                        },
                        "port_given_type_and_tonnage": {
                            "LNG carrier": {
                                "3000": {
                                    "3": 25.000001,
                                    "1": 49.999998,
                                    "2": 25.000001
                                },
                                "1000": {
                                    "3": 100.0
                                },
                                "5000": {
                                    "2": 50.0,
                                    "3": 50.0
                                },
                                "20000": {
                                    "4": 50.0,
                                    "3": 50.0
                                },
                                "150000": {
                                    "2": 100.0
                                },
                                "10000": {
                                    "4": 100.0
                                }
                            }
                        }
                    },
                    "ordinary": {
                        "tonnage_given_type": {
                            "Container ship": {
                                "1000": 9.24672898,
                                "3000": 9.5287174,
                                "50000": 13.29386588,
                                "5000": 22.12986191,
                                "20000": 21.0897704,
                                "150000": 5.88456428,
                                "10000": 12.94192825,
                                "100000": 5.8845629
                            },
                            "General cargo ship": {
                                "1000": 17.17215139,
                                "3000": 17.69583364,
                                "5000": 41.09748863,
                                "10000": 24.03452634
                            },
                            "Dry bulk carrier": {
                                "3000": 27.58620578,
                                "5000": 48.27586174,
                                "1000": 24.13793248
                            },
                            "Product tanker": {
                                "3000": 13.17363878,
                                "5000": 23.05386863,
                                "20000": 23.68402244,
                                "10000": 14.84973272,
                                "50000": 13.71180235,
                                "1000": 11.52693508
                            }
                        },
                        "port_given_type_and_tonnage": {
                            "Container ship": {
                                "1000": {
                                    "5": 33.70832841,
                                    "4": 30.33681017,
                                    "1": 13.00148776,
                                    "3": 8.66766121,
                                    "2": 8.66766121,
                                    "6": 5.61805124
                                },
                                "3000": {
                                    "2": 12.61672779,
                                    "4": 29.43903675,
                                    "5": 16.35538994,
                                    "3": 16.8223011,
                                    "6": 16.35538994,
                                    "1": 8.41115448
                                },
                                "50000": {
                                    "6": 33.1989423,
                                    "3": 27.38268201,
                                    "4": 9.12756352,
                                    "5": 16.59947116,
                                    "2": 13.69134101
                                },
                                "5000": {
                                    "3": 19.9192935,
                                    "5": 28.16933773,
                                    "4": 12.67591559,
                                    "1": 9.05422494,
                                    "6": 21.1270033,
                                    "2": 9.05422494
                                },
                                "20000": {
                                    "4": 20.13734874,
                                    "6": 20.92684169,
                                    "2": 17.26058156,
                                    "5": 24.41464645,
                                    "1": 2.8767618,
                                    "3": 14.38381976
                                },
                                "150000": {
                                    "3": 25.00000584,
                                    "6": 37.49999709,
                                    "2": 12.49999123,
                                    "4": 25.00000584
                                },
                                "10000": {
                                    "2": 10.47116681,
                                    "4": 17.45194687,
                                    "1": 6.98078005,
                                    "5": 32.11183533,
                                    "6": 12.04193731,
                                    "3": 20.94233363
                                },
                                "100000": {
                                    "5": 25.00001169,
                                    "1": 12.49999416,
                                    "6": 37.50000583,
                                    "4": 12.49999416,
                                    "2": 12.49999416
                                }
                            },
                            "General cargo ship": {
                                "1000": {
                                    "5": 33.70832841,
                                    "4": 30.33681017,
                                    "1": 13.00148776,
                                    "3": 8.66766121,
                                    "2": 8.66766121,
                                    "6": 5.61805124
                                },
                                "3000": {
                                    "2": 12.61672779,
                                    "4": 29.43903675,
                                    "5": 16.35538994,
                                    "3": 16.8223011,
                                    "6": 16.35538994,
                                    "1": 8.41115448
                                },
                                "5000": {
                                    "3": 19.9192935,
                                    "5": 28.16933773,
                                    "4": 12.67591559,
                                    "1": 9.05422494,
                                    "6": 21.1270033,
                                    "2": 9.05422494
                                },
                                "10000": {
                                    "2": 10.47116681,
                                    "4": 17.45194687,
                                    "1": 6.98078005,
                                    "5": 32.11183533,
                                    "6": 12.04193731,
                                    "3": 20.94233363
                                }
                            },
                            "Dry bulk carrier": {
                                "3000": {
                                    "2": 18.74999854,
                                    "4": 43.75000438,
                                    "3": 24.99999416,
                                    "1": 12.50000292
                                },
                                "5000": {
                                    "3": 39.28571213,
                                    "4": 25.00000167,
                                    "1": 17.8571431,
                                    "2": 17.8571431
                                },
                                "1000": {
                                    "4": 50.00000001,
                                    "1": 21.42856761,
                                    "3": 14.28571619,
                                    "2": 14.28571619
                                }
                            },
                            "Product tanker": {
                                "3000": {
                                    "2": 18.74999854,
                                    "4": 43.75000438,
                                    "3": 24.99999416,
                                    "1": 12.50000292
                                },
                                "5000": {
                                    "3": 39.28571213,
                                    "4": 25.00000167,
                                    "1": 17.8571431,
                                    "2": 17.8571431
                                },
                                "20000": {
                                    "4": 36.8421094,
                                    "2": 31.5789453,
                                    "1": 5.26315427,
                                    "3": 26.31579103
                                },
                                "10000": {
                                    "2": 18.74999854,
                                    "4": 31.25000146,
                                    "1": 12.50000292,
                                    "3": 37.49999708
                                },
                                "50000": {
                                    "3": 54.54545145,
                                    "4": 18.18182282,
                                    "2": 27.27272573
                                },
                                "1000": {
                                    "4": 50.00000001,
                                    "1": 21.42856761,
                                    "3": 14.28571619,
                                    "2": 14.28571619
                                }
                            }
                        }
                    }
                }
            }
        }
    }
