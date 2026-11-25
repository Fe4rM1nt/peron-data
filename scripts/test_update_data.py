"""Offline tests for number, term and stock matching."""

import unittest
import io
import zipfile
from datetime import date

from update_data import parse_period, stock_name, term_dates, train_numbers, train_directions_at_halinow, train_paths, build_station_catalog, gtfs_station_boards


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.first = date(2026, 11, 27)
        self.last = date(2026, 12, 6)

    def term(self, text):
        return term_dates(text, self.first, self.last, set())

    def test_period(self):
        self.assertEqual(parse_period('Okres obowiązywania 27.11.2026 - 06.12.2026'), (self.first, self.last))
        self.assertEqual(parse_period('Okres obowiązywania: 2026-11-27 - 2026-12-06'), (self.first, self.last))

    def test_numbers(self):
        self.assertEqual(train_numbers('19816/7'), ['19816', '19817'])
        self.assertEqual(train_numbers('91958/9'), ['91958', '91959'])
        self.assertEqual(train_numbers('11121'), ['11121'])

    def test_dates_and_weekdays(self):
        self.assertEqual(self.term('27 XI,4 XII'), {'2026-11-27','2026-12-04'})
        self.assertEqual(self.term('30 XI-3 XII'), {'2026-11-30','2026-12-01','2026-12-02','2026-12-03'})
        self.assertEqual(self.term('27 XI-4 XII (D)'), {'2026-11-27','2026-11-30','2026-12-01','2026-12-02','2026-12-03','2026-12-04'})
        self.assertEqual(self.term('28 XI-6 XII (C)'), {'2026-11-28','2026-11-29','2026-12-05','2026-12-06'})
        self.assertEqual(self.term('27 XI-5 XII (1-6)'), {'2026-11-27','2026-11-28','2026-11-30','2026-12-01','2026-12-02','2026-12-03','2026-12-04','2026-12-05'})
        self.assertEqual(self.term('28 XI,5 XII'), {'2026-11-28','2026-12-05'})
        self.assertEqual(self.term('27 XI-6 XII (Q)'), set())

    def test_stock(self):
        self.assertEqual(stock_name('EN57ALwKM', '2'), '2 × EN57AL')
        self.assertEqual(stock_name('EN57AKMw1', '2'), '2 × EN57AKM')
        self.assertEqual(stock_name('ER160', '2'), '2 × ER160 FLIRT')
        self.assertEqual(stock_name('EU47, P, B, Bs', '1, 3, 1, 1'), 'EU47 + 5 wagonów piętrowych')

    def test_direction_uses_next_stop_not_final_destination(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('stops.txt', 'stop_id,stop_name,stop_lon\nh,Halinów,21.354\nw,Sulejówek Miłosna,21.290\ne,Dębe Wielkie,21.436\n')
            z.writestr('stop_times.txt', 'trip_id,stop_id,stop_sequence\nt1,h,5\nt1,w,6\nt2,h,3\nt2,e,4\nt3,h,2\nt3,w,3\nt4,h,2\nt4,e,3\n')
        data.seek(0)
        trips = {'t1': ('KM', ['93482'], 'daily'), 't2': ('KM', ['19813'], 'daily'),
                 't3': ('KM', ['99999'], 'daily'), 't4': ('KM', ['99999'], 'daily')}
        with zipfile.ZipFile(data) as z:
            result = train_directions_at_halinow(z, trips, {'daily': {'2026-11-25'}})
        self.assertEqual(result['2026-11-25']['KM']['93482'], 'west')
        self.assertEqual(result['2026-11-25']['KM']['19813'], 'east')
        self.assertNotIn('99999', result['2026-11-25']['KM'])

    def test_station_paths_resolve_next_stop_at_any_station(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('stops.txt', 'stop_id,stop_name,parent_station\nh,Halinów,\nw,Warszawa Zachodnia,\np1,Peron 1,h\np2,Peron 2,w\nl,Łowicz Główny,\n')
            z.writestr('stop_times.txt', 'trip_id,stop_id,stop_sequence\nt1,p1,1\nt1,p2,2\nt1,l,3\n')
        data.seek(0)
        with zipfile.ZipFile(data) as z:
            paths, names = train_paths(z, {'t1': ('KM',['93482'],'daily')}, {'daily': {'2026-11-25'}})
        self.assertEqual(paths['2026-11-25']['KM']['93482'], ['h','w','l'])
        self.assertEqual(names['w'], 'Warszawa Zachodnia')

    def test_catalog_contains_only_stations_served_by_km_or_skm(self):
        stops = {str(n): f'Przystanek {n}' for n in range(1, 90)}
        stops['hal'] = 'Halinów'
        stations = build_station_catalog(stops)
        names = {s['name'] for s in stations}
        self.assertIn('Przystanek 17', names)
        self.assertIn('Halinów', names)
        self.assertEqual(next(s['id'] for s in stations if s['name']=='Przystanek 17'), 'gtfs:17')
        self.assertNotIn('Przypadkowa Stacja', names)

    def test_gtfs_boards_include_only_serviced_stops_with_correct_direction(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('stops.txt', 'stop_id,stop_name,parent_station,stop_lon\nh,Halinów,,21.354\nw,Sulejówek Miłosna,,21.290\nl,Łowicz Główny,,20.200\np1,Peron 1,h,21.354\n')
            z.writestr('stop_times.txt', 'trip_id,stop_id,stop_sequence,arrival_time,departure_time,platform_code\nt1,p1,1,15:09:00,15:10:00,II\nt1,w,2,15:19:00,15:20:00,I\nt1,l,3,16:55:00,16:55:00,I\n')
        data.seek(0)
        with zipfile.ZipFile(data) as z:
            boards = gtfs_station_boards(z, {'t1': ('KM', ['93482'], 'daily')},
                                          {'t1': 'R3'}, {'daily': {'2026-11-25'}},
                                          {'h': 'Halinów', 'w': 'Sulejówek Miłosna', 'l': 'Łowicz Główny'},
                                          date(2026, 11, 25))
        train = boards['h']['departures']['2026-11-25'][0]
        self.assertEqual(train['trainCode'], 'KM 93482')
        self.assertEqual(train['nextStop'], 'Sulejówek Miłosna')
        self.assertEqual(train['arrivalStation'], 'Łowicz Główny')
        self.assertEqual(train['direction'], 'west')
        self.assertEqual(train['platform'], 'II')
        arrival = boards['l']['arrivals']['2026-11-25'][0]
        self.assertEqual(arrival['trainCode'], 'KM 93482')
        self.assertEqual(arrival['originStation'], 'Halinów')
        self.assertEqual(arrival['prevStop'], 'Sulejówek Miłosna')
        self.assertTrue(arrival['terminates'])
        self.assertNotIn('2026-11-25', boards['l']['departures'])
        intermediate = boards['w']['arrivals']['2026-11-25'][0]
        self.assertFalse(intermediate['terminates'])
        self.assertEqual(intermediate['prevStop'], 'Halinów')


if __name__ == '__main__':
    unittest.main()
