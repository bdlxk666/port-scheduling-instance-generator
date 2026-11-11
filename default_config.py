"""Editable instance-generation parameters. All times are minutes from midnight unless noted."""

def default_config():
    channel_rows = [
        ('AB',7.4,10),('BC',4.3,13),('CD',2.6,9),('BE',4.2,7),
        ('BF',3.2,7),('BG',2.7,7),('HD',9,7),('BH',3.5,7),
        ('BJ',4,7),('DK',1.4,9),('DL',2,9)]
    # Size intervals are (lower, upper]; the first class includes its lower end.
    classes = {
        'Container ship': [(1000,2000,26.43),(2000,10000,6.21),(10000,50000,16.56),(50000,100000,11.31),(100000,150000,39.49)],
        'General cargo ship': [(1000,2000,14.02),(2000,5000,82.23),(5000,10000,3.75)],
        'Dry bulk carrier': [(1000,2000,12.5),(2000,5000,87.5)],
        'Product tanker': [(1,1000,4.51),(1000,5000,25.56),(5000,10000,45.86),(10000,50000,24.06)],
        'LNG carrier': [(1,99999,100)],
    }
    means = [0.7879,3.5177,16.3621,7.6213,7.7376]
    # Compatible berth classes by port and vessel type.
    berth_classes = {
        'P1': {'LNG carrier':[100000]}, 'P2': {'LNG carrier':[100000]},
        'P3': {'Dry bulk carrier':[2000,5000], 'General cargo ship':[2000,5000]},
        'P4': {'Product tanker':[1000,5000,10000,50000],'LNG carrier':[100000]},
        'P5': {'Container ship':[2000,10000,50000,100000,150000],'General cargo ship':[2000,5000,10000]},
        'P6': {'Container ship':[2000,10000,50000,100000,150000],'General cargo ship':[2000,5000,10000]},
    }
    tails = {'P1':['BG'],'P2':['BF'],'P3':['BJ:before4','4','BJ:after4'],
             'P4':['BE:before4','4','BE:after4'],
             'P5':['BC','3','CD','1','DK'],'P6':['BC','3','CD','1','DL']}
    old_weights = {'P1':[.6,.4],'P2':[.7,.3],'P3':[.5,.25],
                   'P4':[.5,.25],'P5':[.35,.35,.1],'P6':[.35,.35,.1]}
    old_indices = {'P1':[1,2],'P2':[1,2],'P3':[1,2],
                   'P4':[1,2],'P5':[1,4,3],'P6':[1,4,3]}
    routes = []
    for port, tail in tails.items():
        paths = [['AB','5']+tail, ['2','BH','5']+tail]
        if port in ('P5','P6'):
            paths.append(['2','HD','1', 'DK' if port=='P5' else 'DL'])
        for n,(path,w,old) in enumerate(zip(paths,old_weights[port],old_indices[port]),1):
            routes.append(dict(port=port,route_id=f'{port}-R{n}',legacy_route=old,
                               weight=w,steps=path))
    return dict(
        version='1.0.0', base_seed=20261105, cases_per_density=5, max_attempts=20000,
        density_bands={'L':[20,25],'M':[28,35],'H':[36,45]},
        horizon_start=360,horizon_end=720,previous_start=0,
        peak_daily_share=.35,inbound_share=.5,autonomous_share=.3,
        autonomous_eligible_types=['Container ship','General cargo ship','Dry bulk carrier','Product tanker'],
        inherited_count_range=[3,5],berth_pair_fraction=.15,berth_clearance_min=1,
        safety_interval_min=5,non_lng_delay_weight=1,lng_delay_weight=3,
        port_weights={'P1':.1,'P2':.15,'P3':.2,'P4':.2,'P5':.15,'P6':.15},
        channels=[dict(channel=n,length_nm=l,speed_kn=v,one_way=n!='AB') for n,l,v in channel_rows],
        areas=[dict(area=str(i),capacity=cap,safety_factor=sf,occupancy_min=t)
               for i,cap,sf,t in [(1,2,1,15),(2,1,1,5),(3,1,1,10),(4,1,1,10),(5,6,.9,20)]],
        via4_channel_fraction={'BJ':.5,'BE':.5},
        vessels=[dict(vessel_type=k,mean_interarrival_h=m,size_classes=[
            dict(lower_t=a,upper_t=b,weight=w) for a,b,w in classes[k]])
                 for k,m in zip(classes,means)],
        berth_classes=berth_classes,routes=routes,
        )
