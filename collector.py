"""GRIB2 规则经纬网实际数据采集（本地限定范围）。

支持范围（超出范围的场被拒绝，同消息后续场继续解析）：
- 第 3 段模板 3.0：规则经纬网（Template 3.0 / latitude-longitude）
- 第 4 段模板 4.0：确定性预报（Template 4.0 / analysis or forecast at a horizontal level）
- 第 5 段模板 5.0：simple packing
- 第 6 段位图指示符 0（本段位图）、255（无位图，全有效）、254（复用同消息先前位图）

设计要点：
- 每条 GRIB 消息独立维护“当前网格/产品/表示模板/位图”状态，消息之间绝不复用；
- 打包值个数（第 5 段第 4 组计数）只等于位图有效点数，不能当作网格总点数；
- 格点索引按网格逻辑行列稳定编号（i=0..Ni-1, j=0..Nj-1），与扫描方向无关；
- 坐标与物理量缩放全部由模板字段计算，不写死任何样例数值；
- 单场解析失败只拒绝该场，记录原因，不影响后续消息的检查与解析。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import struct

GRIB_MAGIC = b'GRIB'
GRIB_END = b'7777'
SUPPORTED_EDITION = 2

GRID_TEMPLATE = 0
PRODUCT_TEMPLATE = 0
PACKING_TEMPLATE = 0

BITMAP_PRESENT = 0
BITMAP_REUSE = 254
BITMAP_NONE = 255

# WMO GRIB2 表 4.4：指示预报时间的时间单位
TIME_UNIT_HOUR = 1
TIME_UNIT_MINUTE = 0
TIME_UNIT_SECOND = 13
TIME_UNIT_DAY = 2
TIME_UNIT_MONTH = 3
TIME_UNIT_YEAR = 4
TIME_UNIT_6H = 10
TIME_UNIT_12H = 11
TIME_UNIT_3H = 12


class GribError(ValueError):
    """原件结构不满足 GRIB2 约定（影响整条消息的继续解析）。"""


class FieldRejected(Exception):
    """单个场不被支持或自相矛盾；拒绝该场但保留后续消息可检查。"""

    def __init__(self, reason, section=None):
        super().__init__(reason)
        self.reason = reason
        self.section = section


@dataclass(frozen=True)
class Point:
    """单个格点的采集结果。index 是与扫描方向无关的稳定网格索引。"""

    index: int
    i: int
    j: int
    latitude: float
    longitude: float          # 模板原始经度，可能为 361 这类跨零数值
    longitude_signed: float   # 归一化到 [-180, 180)
    value: float | None
    missing_reason: str | None


@dataclass
class ParsedField:
    message_index: int
    field_index: int
    grid: dict
    product: dict
    times: dict
    points: list
    provenance: dict
    identity_key: str
    section_offsets: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# 底层读取
# --------------------------------------------------------------------------

def _u1(data, offset):
    return data[offset]


def _u2(data, offset):
    return struct.unpack_from('>H', data, offset)[0]


def _u4(data, offset):
    return struct.unpack_from('>I', data, offset)[0]


def _u8(data, offset):
    return struct.unpack_from('>Q', data, offset)[0]


def _signed_magnitude(raw, width):
    """GRIB 缩放/坐标的有符号量采用符号位 + 幅值，不是二进制补码。"""

    sign_bit = 1 << (width - 1)
    if raw & sign_bit:
        return -(raw & (sign_bit - 1))
    return raw


def _s4(data, offset):
    return _signed_magnitude(_u4(data, offset), 32)


def _s2(data, offset):
    return _signed_magnitude(_u2(data, offset), 16)


def _s1(data, offset):
    return _signed_magnitude(_u1(data, offset), 8)


def _require(condition, reason, section=None):
    if not condition:
        raise FieldRejected(reason, section)


# --------------------------------------------------------------------------
# 消息与段切分（沿用 inspect 的结构校验，返回带偏移的段表）
# --------------------------------------------------------------------------

def split_messages(data):
    """把原件切成 (消息序号, 起始偏移, 长度, [段描述])，结构错误直接抛 GribError。"""

    messages = []
    offset = 0
    message_index = 0
    while offset < len(data):
        if data[offset:offset + 4] != GRIB_MAGIC or len(data) - offset < 20:
            raise GribError('GRIB 消息头缺失或截断')
        if data[offset + 7] != SUPPORTED_EDITION:
            raise GribError('只支持 GRIB edition 2')
        length = _u8(data, offset + 8)
        end = offset + length
        if length < 20 or end > len(data) or data[end - 4:end] != GRIB_END:
            raise GribError('GRIB 消息长度或尾标记不完整')
        position = offset + 16
        sections = []
        while position < end - 4:
            if position + 5 > end - 4:
                raise GribError('GRIB 段截断')
            size = _u4(data, position)
            number = data[position + 4]
            if size < 5 or position + size > end - 4:
                raise GribError('GRIB 段越界')
            sections.append({'number': number, 'offset': position,
                             'size': size, 'body_start': position + 5})
            position += size
        messages.append({'index': message_index, 'offset': offset,
                         'length': length, 'end': end, 'sections': sections})
        offset = end
        message_index += 1
    if not messages:
        raise GribError('原件中没有 GRIB 消息')
    return messages


def _section_payload(data, section):
    start = section['body_start']
    return data[start:start + section['size'] - 5]


# --------------------------------------------------------------------------
# 第 1 段：标识与起报时间
# --------------------------------------------------------------------------

def parse_identification(data, section):
    p = section['body_start']
    # 模板体（16 字节）：中心(2) 子中心(2) 表版本(1) 本地表(1) 起报意义(1)
    # 年(2) 月(1) 日(1) 时(1) 分(1) 秒(1) 状态(1) 数据类型(1)
    year, month, day, hour, minute, second = struct.unpack_from('>HBBBBB', data, p + 7)
    try:
        reference = datetime(year, month, day, hour, minute, second)
    except ValueError as error:
        raise FieldRejected('起报时间字段无效: %s' % error, section=1)
    return {
        'originating_center': _u2(data, p),
        'originating_subcenter': _u2(data, p + 2),
        'tables_version': data[p + 4],
        'local_tables_version': data[p + 5],
        'reference_time_significance': data[p + 6],
        'reference_time': reference,
        'reference_time_iso': reference.isoformat(),
        'production_status': data[p + 14],
        'data_type': data[p + 15],
    }


# --------------------------------------------------------------------------
# 第 3 段：模板 3.0 规则经纬网
# --------------------------------------------------------------------------

def parse_grid(data, section):
    _require(section['size'] - 5 >= 14, '第 3 段模板体过短', section=3)
    p = section['body_start']
    # WMO 第 3 段体：偏移0 定义来源(1) 1 格点总数(4) 5 每点附加字节(1)
    # 6 解释(1) 7 保留(1) 8 模板号(2)，模板 3.0 体自偏移 10 起
    source_of_definition = data[p]
    _require(source_of_definition == 0,
             '不支持引用外部先前定义的网格（第 3 段定义来源=%s），本地仅接受本段内定义'
             % source_of_definition, section=3)
    number_points = _u4(data, p + 1)
    octets_for_points = data[p + 5]
    interpretation = data[p + 6]
    template_number = _u2(data, p + 8)
    _require(template_number == GRID_TEMPLATE,
             '不支持的第 3 段网格模板: %s（本地仅支持规则经纬网 3.0）' % template_number,
             section=3)
    t = p + 10
    _require(section['size'] - 5 >= t - p + 48, '第 3 段模板 3.0 字段不完整', section=3)

    shape = data[t]
    scale_factor_sphere = data[t + 1]
    scaled_value_sphere = _u4(data, t + 2)
    ni = _u4(data, t + 6)
    nj = _u4(data, t + 10)
    basic_angle = _u4(data, t + 14)
    angle_subdivisions = _u4(data, t + 18)
    first_lat = _s4(data, t + 22)
    first_lon = _u4(data, t + 26)
    resolution = data[t + 30]
    last_lat = _s4(data, t + 31)
    last_lon = _u4(data, t + 35)
    i_direction_increment = _s4(data, t + 39)
    j_direction_increment = _s4(data, t + 43)
    scan_mode = data[t + 47]

    _require(ni > 0 and nj > 0, '网格行列数必须为正', section=3)
    _require(number_points == ni * nj,
             '第 3 段格点总数 %s 与行列乘积 %s×%s 不一致' % (number_points, ni, nj),
             section=3)
    _require(octets_for_points == 0,
             '不支持随格点附带的可选坐标值（Ni*Nj 附加八进制数=%s）' % octets_for_points,
             section=3)
    _require(interpretation == 0, '不支持的可选坐标列表解释方式: %s' % interpretation,
             section=3)

    # 角度换算单位：基本角与细分均为 0 时按 10^-6 度，否则按 基本角/细分 度
    if basic_angle == 0 and angle_subdivisions == 0:
        angle_unit = 1e-6
    else:
        _require(angle_subdivisions != 0, '角度基本角细分不能为 0', section=3)
        angle_unit = basic_angle / angle_subdivisions

    # 模板内 i/j 增量为无符号幅值；行进方向完全由扫描模式位决定
    di = abs(i_direction_increment) * angle_unit
    dj = abs(j_direction_increment) * angle_unit
    _require(di > 0 and dj > 0, '经纬向增量必须为正', section=3)

    first_lat_deg = first_lat * angle_unit
    first_lon_deg = first_lon * angle_unit
    last_lat_deg = last_lat * angle_unit
    last_lon_deg = last_lon * angle_unit

    # 扫描模式（WMO 表 3.4）：
    # bit1(0x80)=0 行内 i 自西向东（经度递增），=1 递减
    # bit2(0x40)=0 列方向 j 自北向南（纬度递减），=1 递增
    # bit3(0x20)=1 相邻经线上 i 原点交替（本实现不支持）
    scan_i_negative = bool(scan_mode & 0x80)
    scan_j_positive = bool(scan_mode & 0x40)
    adjacent_rows_offset = bool(scan_mode & 0x20)
    _require(not adjacent_rows_offset,
             '不支持相邻经线扫描方向交替（扫描模式 0x%02x bit3）' % scan_mode, section=3)

    expected_last_i = first_lon_deg + (-di if scan_i_negative else di) * (ni - 1)
    # bit2=0 向南（纬度递减）；bit2=1 向北（纬度递增）
    j_sign = 1 if scan_j_positive else -1
    expected_last_j = first_lat_deg + j_sign * dj * (nj - 1)
    _require(abs(expected_last_i - last_lon_deg) < 1e-9,
             'i 方向端点经度与增量/扫描方向不一致（模板声明 %s，按增量推算 %s）'
             % (last_lon_deg, expected_last_i), section=3)
    _require(abs(expected_last_j - last_lat_deg) < 1e-9,
             'j 方向端点纬度与增量/扫描方向不一致（模板声明 %s，按增量推算 %s）'
             % (last_lat_deg, expected_last_j), section=3)

    return {
        'template': template_number,
        'source_of_definition': source_of_definition,
        'number_points': number_points,
        'shape': shape,
        'scale_factor_sphere': scale_factor_sphere,
        'scaled_value_sphere': scaled_value_sphere,
        'ni': ni,
        'nj': nj,
        'angle_unit_degrees': angle_unit,
        'first_latitude': first_lat_deg,
        'first_longitude': first_lon_deg,
        'last_latitude': last_lat_deg,
        'last_longitude': last_lon_deg,
        'i_direction_increment': di,
        'j_direction_increment': dj,
        'resolution_component_flags': resolution,
        'scan_mode': scan_mode,
        'scan_i_negative': scan_i_negative,
        'scan_j_positive': scan_j_positive,
    }


def _normalize_signed_longitude(longitude):
    normalized = longitude % 360.0
    if normalized >= 180.0:
        normalized -= 360.0
    return normalized


def grid_coordinates(grid, i, j):
    """计算逻辑 (i, j) 格点的经纬度。

    逻辑坐标约定固定且与扫描方向无关：i=0 在最西、自西向东（经度 +di）；
    j=0 在最北、自北向南（纬度 -dj）。因此稳定索引 j*Ni+i 不随扫描模式
    变化。网格西北角坐标由模板首点和扫描位反推：
      - i 反向扫描时首点在最东，西北角经度 = 首点经度 - (Ni-1)*di；
      - j 向北扫描(bit2=1)时首点在最南，西北角纬度 = 首点纬度 + (Nj-1)*dj。
    全部由模板字段计算，不写死任何数值。
    """

    di = grid['i_direction_increment']
    dj = grid['j_direction_increment']
    if grid['scan_i_negative']:
        northwest_lon = grid['first_longitude'] - di * (grid['ni'] - 1)
    else:
        northwest_lon = grid['first_longitude']
    if grid['scan_j_positive']:
        northwest_lat = grid['first_latitude'] + dj * (grid['nj'] - 1)
    else:
        northwest_lat = grid['first_latitude']
    longitude = northwest_lon + i * di
    latitude = northwest_lat - j * dj
    return latitude, longitude


# --------------------------------------------------------------------------
# 第 4 段：模板 4.0 确定性预报
# --------------------------------------------------------------------------

def parse_product(data, section):
    _require(section['size'] - 5 >= 8, '第 4 段模板体过短', section=4)
    p = section['body_start']
    # WMO 第 4 段体：偏移0 坐标值数(2) 2 模板号(2) 4 保留(4)，模板 4.0 体自偏移 8 起
    coordinate_values_count = _u2(data, p)
    template_number = _u2(data, p + 2)
    _require(template_number == PRODUCT_TEMPLATE,
             '不支持的第 4 段产品模板: %s（本地仅支持确定性预报 4.0）' % template_number,
             section=4)
    t = p + 8
    _require(coordinate_values_count == 0,
             '不支持带坐标值的第 4 段（坐标值数=%s）' % coordinate_values_count, section=4)
    _require(section['size'] - 5 >= t - p + 25, '第 4 段模板 4.0 字段不完整', section=4)

    parameter_category = data[t]
    parameter_number = data[t + 1]
    type_of_process = data[t + 2]
    background_process = data[t + 3]
    generating_process = data[t + 4]
    hours_after_cutoff = _u2(data, t + 5)
    minutes_after_cutoff = data[t + 7]
    indicator_of_unit = data[t + 8]
    forecast_time = _u4(data, t + 9)
    type_of_first_fixed_surface = data[t + 13]
    scale_factor_first_surface = _s1(data, t + 14)
    scaled_value_first_surface = _s4(data, t + 15)
    type_of_second_fixed_surface = data[t + 19]
    scale_factor_second_surface = _s1(data, t + 20)
    scaled_value_second_surface = _s4(data, t + 21)
    return {
        'template': template_number,
        'discipline': None,  # 由第 0 段 discipline 填充
        'parameter_category': parameter_category,
        'parameter_number': parameter_number,
        'type_of_generating_process': type_of_process,
        'background_process': background_process,
        'generating_process_identifier': generating_process,
        'hours_after_data_cutoff': hours_after_cutoff,
        'minutes_after_data_cutoff': minutes_after_cutoff,
        'indicator_of_unit_of_time': indicator_of_unit,
        'forecast_time': forecast_time,
        'type_of_first_fixed_surface': type_of_first_fixed_surface,
        'scale_factor_of_first_fixed_surface': scale_factor_first_surface,
        'scaled_value_of_first_fixed_surface': scaled_value_first_surface,
        'type_of_second_fixed_surface': type_of_second_fixed_surface,
        'scale_factor_of_second_fixed_surface': scale_factor_second_surface,
        'scaled_value_of_second_fixed_surface': scaled_value_second_surface,
    }


def _add_forecast_duration(reference, unit, value):
    if unit == TIME_UNIT_MINUTE:
        return reference + timedelta(minutes=value)
    if unit == TIME_UNIT_HOUR:
        return reference + timedelta(hours=value)
    if unit == TIME_UNIT_SECOND:
        return reference + timedelta(seconds=value)
    if unit == TIME_UNIT_DAY:
        return reference + timedelta(days=value)
    if unit == TIME_UNIT_3H:
        return reference + timedelta(hours=3 * value)
    if unit == TIME_UNIT_6H:
        return reference + timedelta(hours=6 * value)
    if unit == TIME_UNIT_12H:
        return reference + timedelta(hours=12 * value)
    if unit == TIME_UNIT_MONTH:
        month_index = (reference.month - 1) + value
        year = reference.year + month_index // 12
        month = month_index % 12 + 1
        return _safe_replace_month(reference, year, month)
    if unit == TIME_UNIT_YEAR:
        return _safe_replace_month(reference, reference.year + value, reference.month)
    raise FieldRejected('不支持的预报时间单位（表 4.4）: %s' % unit, section=4)


def _safe_replace_month(reference, year, month):
    day = min(reference.day, 28)
    return reference.replace(year=year, month=month, day=day)


def build_times(identification, product):
    reference = identification['reference_time']
    valid = _add_forecast_duration(reference,
                                   product['indicator_of_unit_of_time'],
                                   product['forecast_time'])
    return {
        'reference_time': reference.isoformat(),
        'reference_time_significance': identification['reference_time_significance'],
        'forecast_time_unit': product['indicator_of_unit_of_time'],
        'forecast_time_value': product['forecast_time'],
        'valid_time': valid.isoformat(),
    }


# --------------------------------------------------------------------------
# 第 5 段：模板 5.0 simple packing
# --------------------------------------------------------------------------

def parse_representation(data, section):
    _require(section['size'] - 5 >= 10, '第 5 段模板体过短', section=5)
    p = section['body_start']
    # WMO 第 5 段体：偏移0 打包值数(4) 4 模板号(2) 6 保留(4)，模板 5.0 体自偏移 10 起
    number_values = _u4(data, p)
    template_number = _u2(data, p + 4)
    _require(template_number == PACKING_TEMPLATE,
             '不支持的第 5 段数据表示模板: %s（本地仅支持 simple packing 5.0）'
             % template_number, section=5)
    t = p + 10
    _require(section['size'] - 5 >= t - p + 10, '第 5 段模板 5.0 字段不完整', section=5)
    reference_value = struct.unpack_from('>f', data, t)[0]
    binary_scale = _s2(data, t + 4)
    decimal_scale = _s2(data, t + 6)
    bits_per_value = data[t + 8]
    field_type = data[t + 9]
    _require(field_type == 0,
             '不支持的现场数据类型: %s（simple packing 仅支持浮点 0）' % field_type,
             section=5)
    _require(bits_per_value > 0, '每个值的位数必须为正（simple packing）', section=5)
    return {
        'template': template_number,
        'number_packed_values': number_values,
        'reference_value': reference_value,
        'binary_scale_factor': binary_scale,
        'decimal_scale_factor': decimal_scale,
        'bits_per_value': bits_per_value,
        'field_type': field_type,
    }


def decode_packed_values(section7_bytes, representation):
    """按 MSB 位序解包 simple packing，并应用 (R + X*2^E)*10^-D。"""

    bits = representation['bits_per_value']
    expected = representation['number_packed_values']
    available_bits = len(section7_bytes) * 8
    usable_bits = (available_bits // bits) * bits
    _require(usable_bits >= expected * bits,
             '第 7 段数据位不足：需要 %s 个 %s 位值，仅有 %s 位'
             % (expected, bits, available_bits), section=7)

    r_value = representation['reference_value']
    e_value = representation['binary_scale_factor']
    d_value = representation['decimal_scale_factor']
    scale = (2.0 ** e_value) * (10.0 ** -d_value)

    values = []
    bit_cursor = 0
    for _ in range(expected):
        accumulator = 0
        remaining = bits
        while remaining:
            byte_index = bit_cursor >> 3
            bit_in_byte = 7 - (bit_cursor & 7)
            take = min(remaining, bit_in_byte + 1)
            shift = bit_in_byte - take + 1
            chunk = (section7_bytes[byte_index] >> shift) & ((1 << take) - 1)
            accumulator = (accumulator << take) | chunk
            bit_cursor += take
            remaining -= take
        values.append((r_value + accumulator * (2.0 ** e_value)) * (10.0 ** -d_value))
    return values


# --------------------------------------------------------------------------
# 第 6 段：位图
# --------------------------------------------------------------------------

def parse_bitmap(data, section, previous_bitmap):
    p = section['body_start']
    _require(section['size'] - 5 >= 1, '第 6 段位图指示符缺失', section=6)
    indicator = data[p]
    if indicator == BITMAP_NONE:
        _require(section['size'] == 6, '无位图指示符 255 不应附带位图数据', section=6)
        return {'indicator': indicator, 'bits': None, 'reused': False,
                'source_section_offset': section['offset']}
    if indicator == BITMAP_PRESENT:
        bitmap_bytes = data[p + 1:p + 1 + section['size'] - 6]
        bits = _expand_bitmap(bitmap_bytes)
        return {'indicator': indicator, 'bits': bits, 'reused': False,
                'source_section_offset': section['offset']}
    if indicator == BITMAP_REUSE:
        _require(section['size'] == 6, '复用位图指示符 254 不应附带位图数据', section=6)
        _require(previous_bitmap is not None,
                 '位图指示符 254 要求同消息先前存在已定义位图，未找到', section=6)
        return {'indicator': indicator, 'bits': previous_bitmap['bits'],
                'reused': True,
                'source_section_offset': previous_bitmap['source_section_offset']}
    raise FieldRejected('不支持的位图指示符: %s' % indicator, section=6)


def _expand_bitmap(bitmap_bytes):
    bits = []
    for byte in bitmap_bytes:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    return bits


# --------------------------------------------------------------------------
# 场装配：网格 + 产品 + 位图 + 数据
# --------------------------------------------------------------------------

def _scan_order_xy(grid, k):
    """把扫描序位置 k 映射为逻辑行列 (i, j)；稳定索引 j*Ni+i 与扫描方向无关。

    逻辑 j=0 在北。bit2=0（j 向南扫描）时扫描首行对应逻辑 j=0；
    bit2=1（j 向北扫描）时扫描首行对应逻辑 j=Nj-1。
    """

    ni = grid['ni']
    nj = grid['nj']
    if grid['scan_i_negative']:
        i = ni - 1 - (k % ni)
    else:
        i = k % ni
    row = k // ni
    if grid['scan_j_positive']:
        j = nj - 1 - row
    else:
        j = row
    return i, j


def _identity_identity_key(identification, grid, product, times):
    material = repr((
        identification['originating_center'],
        identification['originating_subcenter'],
        product['parameter_category'],
        product['parameter_number'],
        product['type_of_generating_process'],
        product['type_of_first_fixed_surface'],
        product['scale_factor_of_first_fixed_surface'],
        product['scaled_value_of_first_fixed_surface'],
        product['type_of_second_fixed_surface'],
        grid['ni'], grid['nj'],
        grid['first_latitude'], grid['first_longitude'],
        grid['i_direction_increment'], grid['j_direction_increment'],
        times['reference_time'], times['valid_time'],
    )).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def assemble_field(message_index, field_index, discipline, identification,
                   grid_section, product_section, representation_section,
                   bitmap_section, data_section, data, previous_bitmap):
    section_offsets = {
        'grid': grid_section['offset'],
        'product': product_section['offset'],
        'representation': representation_section['offset'],
        'bitmap': bitmap_section['offset'],
        'data': data_section['offset'],
    }
    grid = parse_grid(data, grid_section)
    product = parse_product(data, product_section)
    product['discipline'] = discipline
    representation = parse_representation(data, representation_section)
    bitmap = parse_bitmap(data, bitmap_section, previous_bitmap)
    times = build_times(identification, product)

    total_points = grid['number_points']
    packed_count = representation['number_packed_values']

    if bitmap['bits'] is None:
        _require(bitmap['indicator'] == BITMAP_NONE, '位图状态无效', section=6)
        _require(packed_count == total_points,
                 '无位图场的打包值个数 %s 必须等于网格总点数 %s；打包值个数不能当作网格总点数'
                 % (packed_count, total_points), section=5)
        validity = [1] * total_points
        missing_reason_kind = None
    else:
        bits = bitmap['bits']
        _require(len(bits) >= total_points,
                 '位图位数 %s 少于网格总点数 %s' % (len(bits), total_points), section=6)
        bits = bits[:total_points]
        validity = bits
        valid_count = sum(bits)
        _require(valid_count == packed_count,
                 '位图有效点数 %s 与打包值个数 %s 不一致' % (valid_count, packed_count),
                 section=6)

    packed = decode_packed_values(_section_payload(data, data_section), representation)
    _require(len(packed) == packed_count, '解包值个数与第 5 段计数不一致', section=7)

    points = [None] * total_points
    packed_cursor = 0
    for k in range(total_points):
        i, j = _scan_order_xy(grid, k)
        latitude, longitude = grid_coordinates(grid, i, j)
        stable_index = j * grid['ni'] + i
        if validity[k]:
            value = packed[packed_cursor]
            packed_cursor += 1
            reason = None
        else:
            value = None
            reason = 'bitmap_missing'
        points[stable_index] = Point(
            index=stable_index, i=i, j=j,
            latitude=latitude, longitude=longitude,
            longitude_signed=_normalize_signed_longitude(longitude),
            value=value, missing_reason=reason,
        )
    _require(all(point is not None for point in points), '格点装配不完整', section=7)

    provenance = {
        'message_index': message_index,
        'field_index': field_index,
        'message_offset': None,  # 由调用方填充
        'grid_section_offset': grid_section['offset'],
        'product_section_offset': product_section['offset'],
        'representation_section_offset': representation_section['offset'],
        'bitmap_section_offset': bitmap_section['offset'],
        'data_section_offset': data_section['offset'],
        'bitmap_reused': bitmap['reused'],
        'bitmap_source_section_offset': bitmap['source_section_offset'],
        'total_grid_points': total_points,
        'packed_value_count': packed_count,
        'missing_count': total_points - packed_count,
    }
    identity = _identity_identity_key(identification, grid, product, times)
    return ParsedField(
        message_index=message_index, field_index=field_index,
        grid=grid, product=product, times=times,
        points=[point.__dict__ for point in points],
        provenance=provenance, identity_key=identity,
        section_offsets=section_offsets,
    ), bitmap


# --------------------------------------------------------------------------
# 整条原件采集
# --------------------------------------------------------------------------

def collect_bytes(data, source_label=None, file_name=None):
    """采集原件中全部消息的全部场。

    每条消息独立解析状态：段 3/4/5 在消息内可被后续场省略复用，
    位图 254 只允许复用同消息先前位图；消息边界处全部重置，杜绝跨消息误用。
    """

    messages = split_messages(data)
    accepted = []
    rejected = []
    digest = hashlib.sha256(data).hexdigest()

    for message in messages:
        message_index = message['index']
        sections = {(item['number']): item for item in message['sections']}
        ordered = message['sections']
        discipline = data[message['offset'] + 6]

        if 1 not in sections:
            rejected.append(_rejection(message_index, None,
                                       '消息缺少第 1 段标识段', None, message['offset']))
            continue
        try:
            identification = parse_identification(data, sections[1])
        except FieldRejected as error:
            rejected.append(_rejection(message_index, None, error.reason,
                                       error.section, message['offset']))
            continue

        # 消息内状态，消息结束即丢弃
        current_grid = None
        previous_bitmap = None
        field_index = 0
        # 每个场各自的产品(4)/表示(5)/位图(6)/数据(7)；网格(3)允许同消息省略复用
        pending = {'grid': None, 'product': None, 'representation': None,
                  'bitmap': None, 'data': None}

        def flush_field():
            nonlocal field_index, previous_bitmap
            if pending['data'] is None:
                return
            this_field = field_index
            field_index += 1
            # 第 4 段产品定义、第 5 段表示模板每场必备，不允许跨场省略
            missing = [name for name in ('product', 'representation',
                                         'bitmap', 'data')
                       if pending[name] is None]
            if pending['grid'] is None:
                missing.insert(0, 'grid')
            if missing:
                rejected.append(_rejection(
                    message_index, this_field,
                    '场缺少必要段: ' + ','.join('第%s段' % _section_number(name)
                                                for name in missing),
                    None, message['offset']))
                pending['product'] = None
                pending['representation'] = None
                pending['bitmap'] = None
                pending['data'] = None
                return
            try:
                parsed, bitmap_used = assemble_field(
                    message_index, this_field, discipline, identification,
                    pending['grid'], pending['product'], pending['representation'],
                    pending['bitmap'], pending['data'], data, previous_bitmap)
                parsed.provenance['message_offset'] = message['offset']
                accepted.append(parsed)
                previous_bitmap = bitmap_used
            except FieldRejected as error:
                rejected.append(_rejection(
                    message_index, this_field, error.reason, error.section,
                    message['offset'],
                    bitmap_offset=pending['bitmap']['offset']))
            # 每场结束后只清空场级状态；网格 current_grid 保留供同消息后续场复用
            pending['product'] = None
            pending['representation'] = None
            pending['bitmap'] = None
            pending['data'] = None

        for section in ordered:
            number = section['number']
            if number in (0, 1, 8):
                continue
            if number == 3:
                flush_field()
                current_grid = section
                pending['grid'] = section
            elif number == 4:
                pending['product'] = section
            elif number == 5:
                pending['representation'] = section
            elif number == 6:
                pending['bitmap'] = section
            elif number == 7:
                # 仅第 3 段网格允许在同消息内省略并复用最近定义；
                # 第 4/5 段每场必备，缺失会在 flush_field 中拒绝该场。
                if pending['grid'] is None:
                    pending['grid'] = current_grid
                pending['data'] = section
                flush_field()
            else:
                rejected.append(_rejection(
                    message_index, field_index,
                    '不支持的 GRIB 段编号: %s' % number, number, message['offset']))
        flush_field()

    return {
        'source': source_label,
        'file_name': file_name,
        'digest': digest,
        'byte_length': len(data),
        'message_count': len(messages),
        'accepted_field_count': len(accepted),
        'rejected_field_count': len(rejected),
        'fields': [_field_to_dict(item) for item in accepted],
        'rejected': rejected,
    }


def _section_number(slot):
    return {'grid': 3, 'product': 4, 'representation': 5,
            'bitmap': 6, 'data': 7}[slot]


def _rejection(message_index, field_index, reason, section, message_offset,
               bitmap_offset=None):
    record = {
        'message_index': message_index,
        'field_index': field_index,
        'reason': reason,
        'section': section,
        'message_offset': message_offset,
    }
    if bitmap_offset is not None:
        record['bitmap_section_offset'] = bitmap_offset
    return record


def _field_to_dict(parsed):
    return {
        'message_index': parsed.message_index,
        'field_index': parsed.field_index,
        'identity_key': parsed.identity_key,
        'grid': parsed.grid,
        'product': parsed.product,
        'times': parsed.times,
        'points': parsed.points,
        'provenance': parsed.provenance,
        'section_offsets': parsed.section_offsets,
    }


# --------------------------------------------------------------------------
# 港区范围查询
# --------------------------------------------------------------------------

def _longitude_in_range(longitude, west, east):
    """支持跨零经线的经度区间：west <= east 为普通区间，否则为环绕区间。"""

    if west <= east:
        return west <= longitude <= east
    return longitude >= west or longitude <= east


def query_harbor(collected, west, east, south, north,
                 parameter_category=None, parameter_number=None,
                 valid_time=None, use_signed_longitude=True):
    """按港区经纬度框取格点值与缺测原因，结果可追溯到文件、消息与格点位置。

    经度按模板原始值计算（允许 359/361 这类跨零表示），边界为闭区间；
    west > east 表示跨零环绕港区。valid_time 与参数编号可选过滤。
    """

    rows = []
    query_west = _wrap_query_longitude(west, use_signed_longitude)
    query_east = _wrap_query_longitude(east, use_signed_longitude)
    for field in collected['fields']:
        if parameter_category is not None and \
                field['product']['parameter_category'] != parameter_category:
            continue
        if parameter_number is not None and \
                field['product']['parameter_number'] != parameter_number:
            continue
        if valid_time is not None and field['times']['valid_time'] != valid_time:
            continue
        grid = field['grid']
        for point in field['points']:
            longitude = point['longitude_signed'] if use_signed_longitude \
                else point['longitude']
            if not _longitude_in_range(longitude, query_west, query_east):
                continue
            if not (south <= point['latitude'] <= north):
                continue
            rows.append({
                'source': collected.get('source'),
                'file_name': collected.get('file_name'),
                'digest': collected.get('digest'),
                'message_index': field['message_index'],
                'field_index': field['field_index'],
                'grid_index': point['index'],
                'grid_i': point['i'],
                'grid_j': point['j'],
                'latitude': point['latitude'],
                'longitude': point['longitude'],
                'longitude_signed': point['longitude_signed'],
                'parameter_category': field['product']['parameter_category'],
                'parameter_number': field['product']['parameter_number'],
                'reference_time': field['times']['reference_time'],
                'valid_time': field['times']['valid_time'],
                'forecast_time_value': field['times']['forecast_time_value'],
                'value': point['value'],
                'missing_reason': point['missing_reason'],
                'provenance': field['provenance'],
                'grid_ni': grid['ni'],
                'grid_nj': grid['nj'],
            })
    return rows


def _wrap_query_longitude(longitude, signed):
    if signed:
        wrapped = longitude % 360.0
        return wrapped - 360.0 if wrapped >= 180.0 else wrapped
    return longitude % 360.0
