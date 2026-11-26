import argparse
import random
from pathlib import Path


class ArrivalStreams(random.Random):
    def __init__(self, network, day_seed, lng_departure_seed, attribute_seed):
        super().__init__(attribute_seed)
        self.day = random.Random(day_seed)
        self.lng_departures = random.Random(lng_departure_seed)
        share = network['traffic']['inbound_share']
        rates = {}
        for vessel in network['vessels']:
            mean = 60 * vessel['mean_interarrival_h']
            for direction, gap in [('in', mean), ('out', mean * share / (1 - share))]:
                rate = 1 / gap
                if rate in rates:
                    raise ValueError('Arrival streams require distinct rates for this network')
                rates[rate] = vessel['name'], direction
        self.lng_rate = next(rate for rate, group in rates.items() if group == ('LNG carrier', 'out'))

    def expovariate(self, rate):
        stream = self.lng_departures if rate == self.lng_rate else self.day
        return stream.expovariate(rate)


def generate(generator, network, sampling, day, lng, attributes):
    rng = ArrivalStreams(network, day, lng, attributes)
    case = generator.make_case(network, sampling, None, rng)
    return case, rng


def split_seed(seed):
    if not 0 <= seed < (1 << 96):
        raise ValueError('The combined seed must be an unsigned 96-bit integer.')
    stream_base = 1 << 32
    day_and_lng, attributes = divmod(seed, stream_base)
    day, lng_departures = divmod(day_and_lng, stream_base)
    return day, lng_departures, attributes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate an instance from its combined seed.')
    parser.add_argument('seeds', metavar='SEED', nargs='+', type=int,
                        help='One combined seed, or the original three stream seeds in order.')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if len(args.seeds) == 1:
        try:
            stream_seeds = split_seed(args.seeds[0])
        except ValueError as exc:
            parser.error(str(exc))
    elif len(args.seeds) == 3:
        stream_seeds = args.seeds
    else:
        parser.error('Supply one combined seed or three original stream seeds.')

    import generate_instances as generator
    network = generator.load_network()
    case, rng = generate(generator, network, generator.load_sampling(), *stream_seeds)
    if case is None or args.output.exists():
        parser.exit(1, 'Check case acceptance and choose a new output path.\n')
    generator.export_xlsx({'Tasks': generator.exported_case(case, rng)}, args.output)
