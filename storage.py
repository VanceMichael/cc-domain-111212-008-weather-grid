import hashlib
import json
import sqlite3

from collector import query_harbor, split_messages


class Archive:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript(
            'CREATE TABLE IF NOT EXISTS captures('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL,'
            'raw BLOB NOT NULL, rows_json TEXT NOT NULL, UNIQUE(source,digest));'
            'CREATE TABLE IF NOT EXISTS fields('
            'id INTEGER PRIMARY KEY,'
            'capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,'
            'message_index INTEGER NOT NULL, field_index INTEGER NOT NULL,'
            'identity_key TEXT NOT NULL, reference_time TEXT NOT NULL,'
            'valid_time TEXT NOT NULL, parameter_category INTEGER,'
            'parameter_number INTEGER, payload_json TEXT NOT NULL,'
            'UNIQUE(capture_id,message_index,field_index));'
            'CREATE TABLE IF NOT EXISTS rejections('
            'id INTEGER PRIMARY KEY,'
            'capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,'
            'message_index INTEGER, field_index INTEGER, reason TEXT NOT NULL,'
            'section INTEGER, payload_json TEXT NOT NULL);'
            'CREATE INDEX IF NOT EXISTS idx_fields_valid ON fields(valid_time);'
            'CREATE INDEX IF NOT EXISTS idx_fields_identity ON fields(identity_key);')

    def save(self, source, raw, rows):
        if not source.strip():
            raise ValueError('来源标识不能为空')
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO captures(source,digest,raw,rows_json) VALUES(?,?,?,?)',
                (source, digest, raw, payload))
            record = self.connection.execute(
                'SELECT id FROM captures WHERE source=? AND digest=?', (source, digest)).fetchone()
        return record[0]

    def list(self):
        return [dict(id=row[0], source=row[1], digest=row[2], rows=json.loads(row[3]))
                for row in self.connection.execute(
                    'SELECT id,source,digest,rows_json FROM captures ORDER BY id')]

    def raw(self, capture_id):
        row = self.connection.execute('SELECT raw FROM captures WHERE id=?', (capture_id,)).fetchone()
        if row is None:
            raise KeyError(capture_id)
        return bytes(row[0])

    # ------------------------------------------------------------------
    # 业务采集：场的持久化与港区查询
    # ------------------------------------------------------------------

    def save_collection(self, source, raw, collected):
        """保存原件及其解析场。

        原件按 (source,digest) 幂等；场按 (capture, 消息序, 场序) 幂等。
        同一有效时刻但起报时间不同的批次 identity_key 不同，因此各自独立保存，
        不会被当作重复数据。
        """

        capture_id = self.save(source, raw, [
            {'offset': item['offset'], 'length': item['length']}
            for item in split_messages(raw)
        ])
        inserted_fields = 0
        inserted_rejections = 0
        file_name = collected.get('file_name')
        with self.connection:
            for field in collected['fields']:
                existing = self.connection.execute(
                    'SELECT 1 FROM fields WHERE capture_id=? AND message_index=? AND field_index=?',
                    (capture_id, field['message_index'], field['field_index'])).fetchone()
                if existing:
                    continue
                field['provenance']['file_name'] = file_name
                field['provenance']['source'] = source
                self.connection.execute(
                    'INSERT INTO fields(capture_id,message_index,field_index,identity_key,'
                    'reference_time,valid_time,parameter_category,parameter_number,payload_json)'
                    ' VALUES(?,?,?,?,?,?,?,?,?)',
                    (capture_id, field['message_index'], field['field_index'],
                     field['identity_key'], field['times']['reference_time'],
                     field['times']['valid_time'],
                     field['product']['parameter_category'],
                     field['product']['parameter_number'],
                     json.dumps(field, ensure_ascii=False, sort_keys=True)))
                inserted_fields += 1
            for rejection in collected['rejected']:
                self.connection.execute(
                    'INSERT INTO rejections(capture_id,message_index,field_index,'
                    'reason,section,payload_json) VALUES(?,?,?,?,?,?)',
                    (capture_id, rejection['message_index'], rejection['field_index'],
                     rejection['reason'], rejection.get('section'),
                     json.dumps(rejection, ensure_ascii=False, sort_keys=True)))
                inserted_rejections += 1
        return {
            'capture_id': capture_id,
            'inserted_fields': inserted_fields,
            'inserted_rejections': inserted_rejections,
            'field_rows': self.connection.execute(
                'SELECT COUNT(*) FROM fields WHERE capture_id=?', (capture_id,)).fetchone()[0],
            'rejection_rows': self.connection.execute(
                'SELECT COUNT(*) FROM rejections WHERE capture_id=?', (capture_id,)).fetchone()[0],
        }

    def list_fields(self, valid_time=None, parameter_category=None,
                    parameter_number=None):
        """列出已保存场，可按有效时刻/物理量过滤；起报批次始终分别保留。"""

        sql = ('SELECT f.payload_json, c.source, c.digest FROM fields f '
               'JOIN captures c ON c.id=f.capture_id WHERE 1=1')
        arguments = []
        if valid_time is not None:
            sql += ' AND f.valid_time=?'
            arguments.append(valid_time)
        if parameter_category is not None:
            sql += ' AND f.parameter_category=?'
            arguments.append(parameter_category)
        if parameter_number is not None:
            sql += ' AND f.parameter_number=?'
            arguments.append(parameter_number)
        sql += ' ORDER BY f.valid_time, f.reference_time, f.message_index, f.field_index'
        rows = []
        for payload, source, digest in self.connection.execute(sql, arguments):
            field = json.loads(payload)
            field['source'] = source
            field['digest'] = digest
            rows.append(field)
        return rows

    def list_rejections(self):
        return [json.loads(row[0]) for row in self.connection.execute(
            'SELECT payload_json FROM rejections ORDER BY id')]

    def query_harbor(self, west, east, south, north, **filters):
        """跨已保存场执行港区查询，每行带文件、消息与格点位置追溯信息。"""

        result_rows = []
        for field in self.list_fields(
                valid_time=filters.get('valid_time'),
                parameter_category=filters.get('parameter_category'),
                parameter_number=filters.get('parameter_number')):
            single = {
                'source': field['source'],
                'digest': field['digest'],
                'file_name': field['provenance'].get('file_name'),
                'fields': [field],
            }
            for row in query_harbor(
                    single, west, east, south, north,
                    parameter_category=filters.get('parameter_category'),
                    parameter_number=filters.get('parameter_number'),
                    valid_time=filters.get('valid_time')):
                row['file_name'] = field['provenance'].get('file_name')
                result_rows.append(row)
        return result_rows

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
