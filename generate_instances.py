import argparse
import math
import random
import secrets
import tempfile
from collections import defaultdict
from pathlib import Path

from port_model import (
    autonomous_count, export_xlsx, load_network, load_sampling_parameters,
    route_steps, schedule, validate_case,
)


ROUTE_AND_TIDE_WEIGHTS = {
    'L': {
        'ordinary': {
            'route': {
                '3': {'1': 64.285714, '2': 35.714286},
                '2': {'2': 28.571429, '1': 71.428571},
                '5': {'1': 52.173913, '4': 43.478261, '3': 4.347826},
                '1': {'1': 62.5, '2': 37.5},
                '6': {'4': 27.777778, '1': 66.666667, '3': 5.555555},
                '4': {'2': 28.571429, '1': 71.428571},
            },
            'tide': {'1': 48.351648, '0': 51.648352},
        },
        'lng': {
            'route': {
                '4': {'1': 100.0},
                '3': {'1': 100.0},
                '1': {'1': 100.0},
                '2': {'1': 100.0},
            },
            'tide': {'1': 40.0, '0': 60.0},
        },
    },
    'M': {
        'lng': {
            'route': {
                '4': {'1': 100.0},
                '2': {'1': 100.0},
                '3': {'1': 100.0},
                '1': {'1': 100.0},
            },
            'tide': {'0': 46.666667, '1': 53.333333},
        },
        'ordinary': {
            'route': {
                '3': {'1': 56.0, '2': 44.0},
                '6': {'1': 46.153846, '4': 42.307692, '3': 11.538462},
                '4': {'1': 66.666667, '2': 33.333333},
                '2': {'1': 72.727273, '2': 27.272727},
                '5': {'4': 58.064516, '1': 38.709677, '3': 3.225807},
                '1': {'2': 50.0, '1': 50.0},
            },
            'tide': {'0': 45.578231, '1': 54.421769},
        },
    },
    'H': {
        'lng': {
            'route': {
                '3': {'1': 100.0},
                '2': {'1': 100.0},
                '4': {'1': 100.0},
                '1': {'1': 100.0},
            },
            'tide': {'0': 66.666667, '1': 33.333333},
        },
        'ordinary': {
            'route': {
                '5': {'4': 26.829268, '1': 65.853659, '3': 7.317073},
                '2': {'1': 87.5, '2': 12.5},
                '6': {'4': 35.294118, '1': 52.941176, '3': 11.764706},
                '3': {'1': 77.777778, '2': 22.222222},
                '4': {'2': 18.421053, '1': 81.578947},
                '1': {'1': 57.142857, '2': 42.857143},
            },
            'tide': {'1': 45.989305, '0': 54.010695},
        },
    },
}


def load_sampling():
    sampling = load_sampling_parameters()
    for density, profile in sampling['groups'].items():
        for role, weights in profile['roles'].items():
            weights.update(ROUTE_AND_TIDE_WEIGHTS[density][role])
    return sampling


def choose(rng, counts):
    return rng.choices(list(counts), weights=list(counts.values()), k=1)[0]


def berth_capacity(network, port, vessel_type, tonnage):
    return min(cap for cap in network['berth_classes'][port][vessel_type] if cap >= tonnage)


def draw_task(network, profile, role, vessel_id, rng, routes, *, vessel_type, direction):
    weights = profile['roles'][role]
    tonnage_code = choose(rng, weights['tonnage_given_type'][vessel_type])
    port_code = choose(rng, weights['port_given_type_and_tonnage'][vessel_type][tonnage_code])
    port = 'P' + port_code
    tonnage = int(tonnage_code)
    route_code = choose(rng, weights['route'][port_code])
    route = routes[port, int(route_code)]
    steps = route_steps(network, route['route_id'], direction)
    return dict(task_id=vessel_id, port=port, route=route, direction=direction,
                tonnage_t=tonnage, vessel_type=vessel_type,
                steps=steps, travel_min=steps[-1]['exit_offset_min'], predecessor=None,
                berth_id=9000 + vessel_id, lng=int(role == 'lng'), autonomous=0,
                berth_capacity_t=berth_capacity(network, port, vessel_type, tonnage),
                tidal=int(choose(rng, weights['tide'])))


def assign_berths(tasks, preference_exponent, rng):
    if not math.isfinite(preference_exponent) or preference_exponent < 0:
        raise ValueError('berth_preference_exponent must be finite and nonnegative')
    groups = defaultdict(list)
    for task in tasks:
        groups[task['port'], task['vessel_type']].append(task)
    pairs = []
    for vessels in groups.values():
        ranks = rng.sample(range(1, len(vessels) + 1), len(vessels))
        berths = [dict(number=t['berth_id'], capacity=t['berth_capacity_t'],
                       weight=rank ** -preference_exponent, users={})
                  for t, rank in zip(vessels, ranks)]
        order = rng.sample(vessels, len(vessels))
        order.sort(key=lambda t: t['tonnage_t'], reverse=True)
        for task in order:
            direction = task['direction']
            candidates = [b for b in berths if b['capacity'] >= task['tonnage_t']
                          and direction not in b['users'] and not (task['lng'] and b['users'])]
            berth = rng.choices(candidates, weights=[b['weight'] for b in candidates], k=1)[0]
            berth['users'][direction] = task
            task.update(berth_id=berth['number'], berth_capacity_t=berth['capacity'])
            if len(berth['users']) == 2:
                arrival = berth['users']['in']
                departure = berth['users']['out']
                arrival['predecessor'] = departure['task_id']
                pairs.append([arrival['task_id'], departure['task_id']])
    return sorted(pairs)


def arrivals(rng, start, end, mean_minutes):
    if not math.isfinite(mean_minutes) or mean_minutes <= 0:
        raise ValueError('Mean interarrival time must be positive and finite')
    time = start + rng.expovariate(1 / mean_minutes)
    while time < end:
        yield time
        time += rng.expovariate(1 / mean_minutes)


def day_requests(network, rng):
    inbound_share = network['traffic']['inbound_share']
    if not 0 < inbound_share < 1:
        raise ValueError('Inbound share must be between zero and one')
    day = []
    for vessel in network['vessels']:
        mean = 60 * vessel['mean_interarrival_h']
        for direction, gap in (('in', mean), ('out', mean * inbound_share / (1 - inbound_share))):
            for time in arrivals(rng, 0, 1440, gap):
                day.append((time, vessel['name'], direction))
    return sorted(day)


def new_task(network, profile, request, day_id, rng, routes):
    time, kind, direction = request
    task = draw_task(network, profile, 'lng' if kind == 'LNG carrier' else 'ordinary',
                     day_id, rng, routes, vessel_type=kind, direction=direction)
    task.update(raw_min=time, ready_min=math.floor(time))
    return task


def assign_autonomous(network, rng, tasks, waiting=()):
    target = autonomous_count(network, rng, len(tasks) + len(waiting))
    needed = target - sum(t['autonomous'] for t in waiting)
    eligible = [t for t in tasks if not t['lng']]
    if not 0 <= needed <= len(eligible):
        return False
    for t in rng.sample(eligible, needed):
        t['autonomous'] = 1
    return True


def density_for_count(network, count):
    return next((group for group, (lo, hi) in network['density_bands'].items()
                 if lo <= count <= hi), None)


def make_case(network, sampling, density, rng, *, closed=(), targets=None):
    day = day_requests(network, rng)
    boundary, end = network['horizon_start'], network['horizon_end']
    indexed = list(enumerate(day, 1))
    early = [(i, r) for i, r in indexed if r[0] < boundary]
    inside = [(i, r) for i, r in indexed if boundary <= r[0] < end]
    max_lng = max(int(k) for k in sampling['current_lng_counts'])
    if sum(r[1] == 'LNG carrier' for i, r in inside) > max_lng:
        return None
    routes = {(r['port'], r['legacy_route']): r for r in network['routes']}
    profiles = list(sampling['groups'].values())
    previous = [new_task(network, rng.choice(profiles), r, i, rng, routes) for i, r in early]
    if not assign_autonomous(network, rng, previous):
        return None
    prior, prior_events = schedule(network, previous)
    waiting = [dict(t, ready_min=boundary) for t in previous if prior[t['task_id']] >= boundary]
    inherited = [t for t in previous if prior[t['task_id']] < boundary
                 < prior[t['task_id']] + t['travel_min']]
    lo, hi = network['inherited_count_range']
    if not lo <= len(inherited) <= hi:
        return None
    size = len(waiting) + len(inside)
    group = density_for_count(network, size)
    if group is None or group in closed or (density is not None and density != group):
        return None
    lng = sum(t['lng'] for t in waiting) + sum(r[1] == 'LNG carrier' for i, r in inside)
    if str(lng) not in sampling['current_lng_counts'] or (targets and lng != targets[group]):
        return None
    profile = sampling['groups'][group]
    fresh = [new_task(network, profile, r, i, rng, routes) for i, r in inside]
    if not assign_autonomous(network, rng, fresh, waiting):
        return None
    current = [dict(t) for t in waiting] + fresh
    assign_berths(current, network['berth_preference_exponent'], rng)
    inherited_ids = {t['task_id'] for t in inherited}
    fixed = [e for e in prior_events if e['task_id'] in inherited_ids and e['exit'] > boundary]
    starts, _ = schedule(network, current, fixed)
    rng.shuffle(current)
    case = dict(density=group, current=current, previous=previous, inherited=inherited,
                fixed=fixed, prior_start=prior, current_start=starts)
    validate_case(network, case)
    return case


def exported_case(case, rng):
    current = case['current']
    inherited = case['inherited']
    ids = {t['task_id']: i for i, t in enumerate(current + inherited, 1)}
    shared = defaultdict(list)
    for task in current:
        if task['predecessor'] is not None:
            shared[task['port']].append(task)
    berths = {}
    for port, arrivals in shared.items():
        suffixes = rng.sample(range(1, 100), len(arrivals))
        for task, suffix in zip(arrivals, suffixes):
            berths[task['task_id']] = berths[task['predecessor']] = int(port[1:]) * 100 + suffix
    private = [i for i in ids if i not in berths]
    berths.update(zip(private, rng.sample(range(9001, 9001 + len(ids)), len(private))))
    rows = []
    for flag, tasks in ((0, current), (1, inherited)):
        for t in tasks:
            day_id = t['task_id']
            i = ids[day_id]
            berth = berths[day_id]
            minute = case['prior_start'][day_id] if flag else t['ready_min']
            rows.append({
                'Vessel ID': i, 'Ready time (min)': minute, 'Port area': int(t['port'][1:]),
                'Direction': int(t['direction'] == 'out'), 'Tidal window required': t['tidal'],
                'Tonnage class': t['tonnage_t'] / 10000, 'Route ID': t['route']['legacy_route'],
                'LNG carrier': t['lng'], 'Autonomous vessel': t['autonomous'], 'Berth ID': berth,
                'Inherited vessel': flag,
            })
    return rows


def write_batch(network, sampling, count, folder):
    rng = random.SystemRandom()
    groups = sampling['groups']
    if groups.keys() != network['density_bands'].keys():
        raise ValueError('The calibration groups must match the configured density bands')
    counts = dict.fromkeys(groups, 0)
    closed = set()
    targets = {g: int(choose(rng, sampling['current_lng_counts'])) for g in groups}
    for _ in range(max(30000, count * 10000)):
        if len(closed) == len(groups):
            break
        case = make_case(network, sampling, None, rng, closed=closed, targets=targets)
        if case is None:
            continue
        density = case['density']
        counts[density] += 1
        if counts[density] == count:
            closed.add(density)
        else:
            targets[density] = int(choose(rng, sampling['current_lng_counts']))
        name = f'{density}{counts[density]}'
        path = folder / (name + '.xlsx')
        rows = exported_case(case, rng)
        export_xlsx({'Tasks': rows}, path)
    if len(closed) < len(groups):
        raise RuntimeError(f'Candidate limit reached with accepted counts {counts}')


def main():
    parser = argparse.ArgumentParser(description='Generate random port scheduling workbooks.')
    parser.add_argument('--cases-per-density', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.cases_per_density < 1:
        parser.error('--cases-per-density must be positive')
    target = (args.output or Path('generated') / f'run-{secrets.token_hex(6)}').resolve()
    network = load_network()
    sampling = load_sampling()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        parser.error(f'Choose a new or empty output directory: {target}')
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        batch = Path(temporary) / 'batch'
        batch.mkdir()
        write_batch(network, sampling, args.cases_per_density, batch)
        if target.is_dir():
            target.rmdir()
        batch.rename(target)
    print(f'{3 * args.cases_per_density} workbooks | {target}')


if __name__ == '__main__':
    main()
