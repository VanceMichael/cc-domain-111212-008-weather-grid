import json
import tempfile
import unittest
from pathlib import Path

from collector import (collect_bytes, query_harbor, split_messages,
                       GribError)
from sampledata import samples
from storage import Archive


class CollectorFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()

    def test_packed_count_is_not_grid_total(self):
        """打包值个数（第5段）只等于位图有效点数，格点总数以第3段 Ni*Nj 为准。"""

        result = collect_bytes(self.fixtures['sample.grib2'],
                               file_name='sample.grib2')
        field = result['fields'][0]
        grid = field['grid']
        self.assertEqual(grid['ni'] * grid['nj'], 6)
        self.assertEqual(grid['number_points'], 6)
        self.assertEqual(field['provenance']['packed_value_count'], 5)
        self.assertEqual(field['provenance']['total_grid_points'], 6)
        self.assertEqual(field['provenance']['missing_count'], 1)
        # 缺测格点恢复为 None 并带原因，绝不当作零风速
        missing = [p for p in field['points'] if p['value'] is None]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['missing_reason'], 'bitmap_missing')
        self.assertTrue(all(p['value'] != 0 or p['missing_reason'] is not None
                            for p in field['points']))

    def test_times_are_separately_retained(self):
        """起报时间、预报时效、有效时间分别保存。"""

        field = collect_bytes(self.fixtures['harbor-batch-a.grib2'])['fields'][0]
        times = field['times']
        self.assertEqual(times['reference_time'], '2026-11-12T00:00:00')
        self.assertEqual(times['forecast_time_value'], 6)
        self.assertEqual(times['forecast_time_unit'], 1)
        self.assertEqual(times['valid_time'], '2026-11-12T06:00:00')

    def test_same_valid_time_distinct_runs_are_not_duplicates(self):
        """同有效时刻、不同起报批次必须作为不同场，identity_key 不同。"""

        a = collect_bytes(self.fixtures['harbor-batch-a.grib2'])['fields'][0]
        b = collect_bytes(self.fixtures['harbor-batch-b.grib2'])['fields'][0]
        self.assertEqual(a['times']['valid_time'], b['times']['valid_time'])
        self.assertNotEqual(a['times']['reference_time'],
                            b['times']['reference_time'])
        self.assertNotEqual(a['identity_key'], b['identity_key'])

    def test_reverse_scan_yields_stable_indices(self):
        """反向扫描与正向扫描在同一稳定格点索引上坐标、值、缺测完全一致。"""

        forward = collect_bytes(
            self.fixtures['harbor-batch-a.grib2'])['fields'][0]
        reverse = collect_bytes(
            self.fixtures['harbor-reverse-scan.grib2'])['fields'][0]
        self.assertEqual(forward['grid']['scan_mode'], 0x00)
        self.assertEqual(reverse['grid']['scan_mode'], 0xC0)
        self.assertEqual(len(forward['points']), len(reverse['points']))
        for a, b in zip(forward['points'], reverse['points']):
            self.assertEqual(a['index'], b['index'])
            self.assertEqual((a['i'], a['j']), (b['i'], b['j']))
            self.assertAlmostEqual(a['latitude'], b['latitude'])
            self.assertAlmostEqual(a['longitude'], b['longitude'])
            self.assertEqual(a['value'], b['value'])
            self.assertEqual(a['missing_reason'], b['missing_reason'])
        # 索引按 j*Ni+i 稳定且连续
        self.assertEqual([p['index'] for p in forward['points']],
                         list(range(12)))

    def test_scan_order_maps_bitmap_correctly(self):
        """位图按扫描序解释，缺测落到正确逻辑格点而非简单按位置归零。"""

        field = collect_bytes(
            self.fixtures['harbor-batch-a.grib2'])['fields'][0]
        missing_indices = sorted(p['index'] for p in field['points']
                                 if p['missing_reason'] == 'bitmap_missing')
        self.assertEqual(missing_indices, [2, 8])
        # 缺测位置不消耗打包值，相邻有效值仍精确对齐
        by_index = {p['index']: p['value'] for p in field['points']}
        self.assertEqual(by_index[1], 32.0)
        self.assertEqual(by_index[3], 34.0)
        self.assertEqual(by_index[9], 52.0)

    def test_cross_zero_longitude_query_and_trace(self):
        """跨零经线港区查询返回值与缺测原因，并可追溯文件/消息/格点。"""

        collected = collect_bytes(self.fixtures['harbor-batch-a.grib2'],
                                  source_label='partner',
                                  file_name='harbor-batch-a.grib2')
        rows = query_harbor(collected, west=-1.0, east=1.0,
                            south=29.5, north=31.5)
        # 2 个纬度行 × 3 个经度列
        self.assertEqual(len(rows), 6)
        longitudes = sorted({r['longitude_signed'] for r in rows})
        self.assertEqual(longitudes, [-1.0, 0.0, 1.0])
        for row in rows:
            self.assertEqual(row['file_name'], 'harbor-batch-a.grib2')
            self.assertEqual(row['source'], 'partner')
            self.assertIn('digest', row)
            self.assertEqual(row['message_index'], 0)
            self.assertGreaterEqual(row['grid_index'], 0)
            self.assertIn('provenance', row)
        missing = [r for r in rows if r['missing_reason']]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['missing_reason'], 'bitmap_missing')

    def test_wrapping_longitude_range(self):
        """west>east 表达跨零环绕区间。"""

        collected = collect_bytes(self.fixtures['harbor-batch-a.grib2'])
        rows = query_harbor(collected, west=359.5, east=0.5,
                            south=29.0, north=31.0, use_signed_longitude=False)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row['longitude'] >= 359.5 or row['longitude'] <= 0.5)

    def test_reused_bitmap_only_within_message(self):
        """指示符 254 复用同消息先前位图；来源段偏移指向第一场的位图。"""

        result = collect_bytes(self.fixtures['harbor-reused-bitmap.grib2'])
        self.assertEqual(result['accepted_field_count'], 2)
        first, second = result['fields']
        self.assertFalse(first['provenance']['bitmap_reused'])
        self.assertTrue(second['provenance']['bitmap_reused'])
        self.assertEqual(
            second['provenance']['bitmap_source_section_offset'],
            first['provenance']['bitmap_section_offset'])
        # 两场缺测格点位置一致（复用同一位图）
        self.assertEqual(
            [p['index'] for p in first['points'] if p['missing_reason']],
            [p['index'] for p in second['points'] if p['missing_reason']])

    def test_orphan_bitmap_rejected_but_later_message_survives(self):
        """缺少先前位图的场被拒绝，后续消息仍正常解析，且不借用上场网格。"""

        result = collect_bytes(self.fixtures['harbor-orphan-bitmap.grib2'])
        self.assertEqual(result['message_count'], 2)
        self.assertEqual(result['rejected_field_count'], 1)
        self.assertEqual(result['accepted_field_count'], 1)
        self.assertIn('254', result['rejected'][0]['reason'])
        # 后续消息是独立的批次 A，网格来自它自己的第 3 段
        survivor = result['fields'][0]
        self.assertEqual(survivor['message_index'], 1)
        self.assertEqual(survivor['grid']['number_points'], 12)

    def test_unsupported_template_rejected(self):
        """不支持的网格模板拒绝该场，而不是用其他模板硬解。"""

        result = collect_bytes(
            self.fixtures['harbor-unsupported-template.grib2'])
        self.assertEqual(result['accepted_field_count'], 0)
        self.assertEqual(result['rejected_field_count'], 1)
        self.assertIn('网格模板', result['rejected'][0]['reason'])

    def test_invalid_file_raises_structure_error(self):
        """截断原件触发结构性错误，而非产生伪采集结果。"""

        with self.assertRaises(GribError):
            collect_bytes(self.fixtures['invalid.grib2'])

    def test_scaling_computed_from_template(self):
        """物理量缩放 (R + X*2^E)*10^-D 由第5段字段计算，不写死。"""

        from datetime import datetime
        from sampledata import (identification_section, grid_section,
                                 product_section, representation_section,
                                 bitmap_section, data_section, message)
        grid = grid_section(2, 2, 10.0, 100.0, 9.0, 101.0, 1.0, 1.0, 0)
        product = product_section(2, 1, 1, 3)
        representation = representation_section(4, 2.5, 1, 1, 8)
        bitmap = bitmap_section([1, 1, 1, 1], 255)
        data = data_section([0.25, 0.45, 0.65, 0.85], 2.5, 1, 1, 8)
        raw = message([identification_section(datetime(2026, 1, 1)),
                       grid, product, representation, bitmap, data])
        field = collect_bytes(raw)['fields'][0]
        values = [p['value'] for p in field['points']]
        for actual, expected in zip(values, [0.25, 0.45, 0.65, 0.85]):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_coordinates_derive_from_template_not_hardcoded(self):
        """改变模板首点/增量，坐标随之变化，证明没有写死样例经纬度。"""

        from datetime import datetime
        from sampledata import (identification_section, grid_section,
                                 product_section, representation_section,
                                 bitmap_section, data_section, message)
        grid = grid_section(2, 2, 50.0, 10.0, 49.0, 11.0, 1.0, 1.0, 0)
        product = product_section(2, 1, 1, 0)
        representation = representation_section(4, 0.0, 0, 0, 8)
        bitmap = bitmap_section([1, 1, 1, 1], 255)
        data = data_section([1, 2, 3, 4], 0.0, 0, 0, 8)
        raw = message([identification_section(datetime(2026, 1, 1)),
                       grid, product, representation, bitmap, data])
        points = collect_bytes(raw)['fields'][0]['points']
        self.assertEqual(points[0]['latitude'], 50.0)
        self.assertEqual(points[0]['longitude'], 10.0)
        self.assertEqual(points[3]['latitude'], 49.0)
        self.assertEqual(points[3]['longitude'], 11.0)


class StorageCollectionTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()

    def test_collection_persists_and_dedupes_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'store.sqlite3'
            with Archive(database) as archive:
                r1 = archive.save_collection(
                    'partner', self.fixtures['harbor-batch-a.grib2'],
                    collect_bytes(self.fixtures['harbor-batch-a.grib2'],
                                  source_label='partner',
                                  file_name='harbor-batch-a.grib2'))
                r2 = archive.save_collection(
                    'partner', self.fixtures['harbor-batch-b.grib2'],
                    collect_bytes(self.fixtures['harbor-batch-b.grib2'],
                                  source_label='partner',
                                  file_name='harbor-batch-b.grib2'))
                self.assertEqual(r1['field_rows'], 1)
                self.assertEqual(r2['field_rows'], 1)
                fields = archive.list_fields(
                    valid_time='2026-11-12T06:00:00')
                self.assertEqual(len(fields), 2)
                references = {f['times']['reference_time'] for f in fields}
                self.assertEqual(len(references), 2)

    def test_repersisting_same_file_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'store.sqlite3'
            raw = self.fixtures['harbor-reused-bitmap.grib2']
            with Archive(database) as archive:
                first = archive.save_collection(
                    'partner', raw,
                    collect_bytes(raw, file_name='reuse'))
                second = archive.save_collection(
                    'partner', raw,
                    collect_bytes(raw, file_name='reuse'))
                self.assertEqual(first['field_rows'], 2)
                self.assertEqual(second['inserted_fields'], 0)
                self.assertEqual(second['field_rows'], 2)

    def test_rejections_persisted_and_queryable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'store.sqlite3'
            raw = self.fixtures['harbor-orphan-bitmap.grib2']
            with Archive(database) as archive:
                archive.save_collection(
                    'partner', raw, collect_bytes(raw, file_name='orphan'))
                rejections = archive.list_rejections()
                self.assertEqual(len(rejections), 1)
                self.assertIn('254', rejections[0]['reason'])
                harbor = archive.query_harbor(
                    -1.0, 2.0, 29.0, 31.0)
                # 只有幸存消息的场可被港区查询
                self.assertTrue(harbor)
                self.assertTrue(all(r['missing_reason'] in (None, 'bitmap_missing')
                                    for r in harbor))


class StructureSplitTests(unittest.TestCase):
    def test_split_messages_offsets(self):
        data = samples()['harbor-orphan-bitmap.grib2']
        messages = split_messages(data)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]['offset'], 0)
        self.assertEqual(messages[1]['offset'], messages[0]['length'])
        self.assertEqual(data[messages[1]['offset']:
                              messages[1]['offset'] + 4], b'GRIB')


class ProtocolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.ref = __import__('datetime').datetime(2026, 11, 12)
        self.validity = [1, 1, 0, 1, 1, 1, 1, 1, 0, 1, 1, 1]
        self.values = [31, 32, 34, 41, 42, 43, 44, 52, 53, 54]
        self.corners = (31.0, 359.0, 29.0, 362.0, 1.0, 1.0)

    def test_bitmap_254_never_reuses_across_messages(self):
        from sampledata import _wind_field, identification_section, message
        good = samples()['harbor-batch-a.grib2']
        orphan = _wind_field(self.ref, 6, 4, 3, self.corners, 0,
                             self.validity, self.values, bitmap_indicator=254)
        combined = good + message(
            [identification_section(self.ref)] + orphan)
        result = collect_bytes(combined)
        self.assertEqual(result['message_count'], 2)
        self.assertEqual(result['accepted_field_count'], 1)
        self.assertEqual(result['rejected_field_count'], 1)
        self.assertEqual(result['rejected'][0]['message_index'], 1)
        self.assertIn('254', result['rejected'][0]['reason'])

    def test_omitted_grid_section_reused_within_message(self):
        # 协议只允许第 3 段（网格定义）在同消息内省略复用；第 4 段产品定义
        # （含预报时效）每场必须自带，因此第二场保留 4/5 段、仅省略第 3 段。
        from sampledata import _wind_field, identification_section, message
        first = _wind_field(self.ref, 6, 4, 3, self.corners, 0,
                            self.validity, self.values)
        second = _wind_field(self.ref, 12, 4, 3, self.corners, 0,
                             self.validity, [v + 5 for v in self.values],
                             include_grid=False, bitmap_indicator=0)
        raw = message([identification_section(self.ref)] + first + second)
        result = collect_bytes(raw)
        self.assertEqual(result['accepted_field_count'], 2)
        offsets = [f['provenance']['grid_section_offset']
                   for f in result['fields']]
        # 第二场复用第一场的网格段，但产品时效各自独立
        self.assertEqual(offsets[0], offsets[1])
        self.assertEqual(
            result['fields'][0]['times']['forecast_time_value'], 6)
        self.assertEqual(
            result['fields'][1]['times']['forecast_time_value'], 12)

    def test_missing_product_section_is_rejected(self):
        from sampledata import _wind_field, identification_section, message
        first = _wind_field(self.ref, 6, 4, 3, self.corners, 0,
                            self.validity, self.values)
        second = _wind_field(self.ref, 12, 4, 3, self.corners, 0,
                             self.validity, [v + 5 for v in self.values],
                             include_product=False)
        raw = message([identification_section(self.ref)] + first + second)
        result = collect_bytes(raw)
        self.assertEqual(result['accepted_field_count'], 1)
        self.assertEqual(result['rejected_field_count'], 1)
        self.assertIn('第4段', result['rejected'][0]['reason'])


class ValidationRejectionTests(unittest.TestCase):
    def _build(self, grid, packed_count, values, bitmap_indicator=255,
               validity=None):
        from datetime import datetime
        from sampledata import (identification_section, product_section,
                                 representation_section, bitmap_section,
                                 data_section, message)
        product = product_section(2, 1, 1, 0)
        representation = representation_section(packed_count, 0.0, 0, 0, 8)
        bitmap = bitmap_section(validity or [1] * packed_count,
                                bitmap_indicator)
        data = data_section(values, 0.0, 0, 0, 8)
        return message([identification_section(datetime(2026, 1, 1)),
                        grid, product, representation, bitmap, data])

    def test_no_bitmap_requires_packed_equal_total(self):
        from sampledata import grid_section
        grid = grid_section(2, 2, 10.0, 100.0, 9.0, 101.0, 1.0, 1.0, 0)
        raw = self._build(grid, 3, [1, 2, 3])
        result = collect_bytes(raw)
        self.assertEqual(result['accepted_field_count'], 0)
        self.assertIn('打包值个数', result['rejected'][0]['reason'])

    def test_endpoint_inconsistency_rejected(self):
        from sampledata import grid_section
        grid = grid_section(2, 2, 10.0, 100.0, 9.0, 105.0, 1.0, 1.0, 0)
        raw = self._build(grid, 4, [1, 2, 3, 4])
        result = collect_bytes(raw)
        self.assertEqual(result['accepted_field_count'], 0)
        self.assertIn('端点', result['rejected'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
