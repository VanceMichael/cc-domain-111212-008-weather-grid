import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from source import inspect_bytes
from collector import collect_bytes
from sampledata import samples
from storage import Archive
from transport import ReplaySource, fetch

MAX_INPUT = 16*1024*1024

def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data

def demo():
    fixtures = samples()
    name = next(key for key in fixtures if key.startswith('sample.'))
    data = fixtures[name]
    routes = {'GET /sample':[{'base64':base64.b64encode(data).decode()}]}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        status, headers, raw = fetch(server.url + '/sample')
        if status != 200:
            raise ValueError('来源请求失败')
        rows = inspect_bytes(raw)
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            first = archive.save('local-demo', raw, rows)
            repeated = archive.save('local-demo', raw, rows)
            return dict(input=name, bytes=len(raw), captures=len(archive.list()),
                        same_capture=first==repeated, rows=rows, requests=len(server.requests))

def collection_demo():
    """端到端业务采集演示：回环来源 -> 采集 -> 入库 -> 港区查询，覆盖全部要求场景。"""

    fixtures = samples()
    routes = {'GET /' + name: [{'base64': base64.b64encode(data).decode(),
                                'chunk_bytes': 7}]
              for name, data in fixtures.items() if name != 'invalid.grib2'}
    report = {'fetches': 0, 'stored_fields': 0, 'stored_rejections': 0, 'cases': []}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            for name in sorted(name for name in fixtures if name != 'invalid.grib2'):
                status, _, raw = fetch(server.url + '/' + name)
                if status != 200:
                    raise ValueError('来源请求失败: ' + name)
                report['fetches'] += 1
                collected = collect_bytes(raw, source_label='local-demo',
                                           file_name=name)
                saved = archive.save_collection('local-demo', raw, collected)
                report['stored_fields'] += saved['field_rows']
                report['stored_rejections'] += saved['rejection_rows']
                report['cases'].append({
                    'file': name,
                    'messages': collected['message_count'],
                    'accepted': collected['accepted_field_count'],
                    'rejected': collected['rejected_field_count'],
                    'reject_reasons': [item['reason'] for item in collected['rejected']],
                })

            # 同有效时刻的两个起报批次必须各自独立、不判重
            batches = archive.list_fields(valid_time='2026-11-12T06:00:00')
            report['same_valid_time_distinct_batches'] = len({
                field['times']['reference_time'] for field in batches})
            report['batch_identity_keys_distinct'] = len({
                field['identity_key'] for field in batches})

            # 跨零经线港区查询（-1..1 度，覆盖 359E/360E/361E）
            harbor_rows = archive.query_harbor(
                west=-1.0, east=1.0, south=29.5, north=31.5,
                valid_time='2026-11-12T06:00:00')
            report['harbor_row_count'] = len(harbor_rows)
            report['harbor_missing'] = [
                {'file': row['file_name'], 'message': row['message_index'],
                 'grid_index': row['grid_index'], 'reason': row['missing_reason']}
                for row in harbor_rows if row['missing_reason']]
            report['harbor_sample_trace'] = [
                {'source': row['source'], 'digest': row['digest'][:12],
                 'message': row['message_index'], 'field': row['field_index'],
                 'grid_index': row['grid_index'], 'value': row['value']}
                for row in harbor_rows[:2]]

            # 反向扫描与正向扫描在同一稳定格点索引上的值必须一致
            reverse = collect_bytes(fixtures['harbor-reverse-scan.grib2'],
                                    file_name='reverse')['fields'][0]
            forward = collect_bytes(fixtures['harbor-batch-a.grib2'],
                                    file_name='forward')['fields'][0]
            report['reverse_scan_matches_forward'] = all(
                a['i'] == b['i'] and a['j'] == b['j']
                and abs(a['latitude'] - b['latitude']) < 1e-9
                and abs(a['longitude'] - b['longitude']) < 1e-9
                and a['value'] == b['value']
                and a['missing_reason'] == b['missing_reason']
                for a, b in zip(forward['points'], reverse['points']))

            # 坏场被拒绝但后续消息仍可检查
            orphan = collect_bytes(fixtures['harbor-orphan-bitmap.grib2'],
                                   file_name='orphan')
            report['bad_field_isolated'] = (
                orphan['rejected_field_count'] == 1
                and orphan['accepted_field_count'] == 1
                and '254' in orphan['rejected'][0]['reason'])
            report['persisted_rejections'] = len(archive.list_rejections())
    return report

def main(argv=None):
    parser = argparse.ArgumentParser(description='来源检查、原件留存与港区格点采集')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'capture', 'harvest'):
        command = commands.add_parser(name)
        command.add_argument('path')
        if name in ('capture', 'harvest'):
            command.add_argument('--database', required=True)
            command.add_argument('--source', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    fields_cmd = commands.add_parser('fields')
    fields_cmd.add_argument('--database', required=True)
    fields_cmd.add_argument('--valid-time')
    harbor_cmd = commands.add_parser('harbor')
    harbor_cmd.add_argument('--database', required=True)
    harbor_cmd.add_argument('--west', type=float, required=True)
    harbor_cmd.add_argument('--east', type=float, required=True)
    harbor_cmd.add_argument('--south', type=float, required=True)
    harbor_cmd.add_argument('--north', type=float, required=True)
    harbor_cmd.add_argument('--valid-time')
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    commands.add_parser('demo')
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            legacy = demo()
            result = {'legacy_inspection_demo': legacy,
                      'collection_demo': collection_demo()}
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'fields':
            with Archive(args.database) as archive:
                result = archive.list_fields(valid_time=args.valid_time)
        elif args.command == 'harbor':
            with Archive(args.database) as archive:
                result = archive.query_harbor(
                    args.west, args.east, args.south, args.north,
                    valid_time=args.valid_time)
        else:
            raw = read_input(args.path)
            if args.command == 'inspect':
                result = inspect_bytes(raw)
            elif args.command == 'capture':
                rows = inspect_bytes(raw)
                with Archive(args.database) as archive:
                    result = dict(capture_id=archive.save(args.source,raw,rows), rows=len(rows))
            else:  # harvest
                collected = collect_bytes(raw, source_label=args.source,
                                          file_name=Path(args.path).name)
                with Archive(args.database) as archive:
                    result = archive.save_collection(args.source, raw, collected)
                    result['rejected'] = collected['rejected']
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(error=type(error).__name__, reason=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
