import struct

def inspect_bytes(data):
    result = []
    offset = 0
    while offset < len(data):
        if data[offset:offset + 4] != b'GRIB' or len(data) - offset < 20:
            raise ValueError('GRIB 消息头缺失或截断')
        if data[offset + 7] != 2:
            raise ValueError('只支持 GRIB edition 2')
        length = struct.unpack_from('>Q', data, offset + 8)[0]
        end = offset + length
        if end > len(data) or data[end - 4:end] != b'7777':
            raise ValueError('GRIB 消息长度或尾标记不完整')
        position = offset + 16
        sections = []
        while position < end - 4:
            size = struct.unpack_from('>I', data, position)[0]
            if size < 5 or position + size > end - 4:
                raise ValueError('GRIB 段越界')
            sections.append(data[position + 4])
            position += size
        result.append({'offset': offset, 'length': length, 'sections': sections})
        offset = end
    return result
