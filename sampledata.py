"""可重复的本地虚构 GRIB2 夹具生成器（仅标准库）。

所有字节均按 WMO GRIB2 规范的段布局与模板 3.0 / 4.0 / 5.0 编码，
坐标与缩放由调用参数计算，不依赖任何外部资源或第三方库。
保留历史键 sample.grib2 / invalid.grib2，并新增覆盖业务语义的夹具。
"""

import struct


def section(number, body):
    return struct.pack('>IB', len(body) + 5, number) + body


def _signed_magnitude(value, width):
    """GRIB 有符号量：符号位 + 幅值。"""

    limit = (1 << (width - 1)) - 1
    if value < -limit or value > limit:
        raise ValueError('有符号量超出 %s 位范围: %s' % (width, value))
    if value < 0:
        return (-value) | (1 << (width - 1))
    return value


def identification_section(reference):
    body = bytearray(16)
    struct.pack_into('>HH', body, 0, 0, 0)          # 中心、子中心
    body[4] = 2                                      # 主表版本
    body[5] = 0                                      # 本地表版本
    body[6] = 1                                      # 起报时间意义：起报
    struct.pack_into('>HBBBBB', body, 7,
                     reference.year, reference.month, reference.day,
                     reference.hour, reference.minute, reference.second)
    body[14] = 0
    body[15] = 1
    return section(1, bytes(body))


def grid_section(ni, nj, first_lat, first_lon, last_lat, last_lon,
                 di, dj, scan_mode, angle_unit=1e-6):
    """模板 3.0 规则经纬网。角度以 angle_unit（度）为单位转成整数微度语义。"""

    def angle(value):
        return int(round(value / angle_unit))

    body = bytearray()
    body += bytes([0])                               # 网格定义来源（体偏移0）
    body += struct.pack('>I', ni * nj)               # 格点总数（体偏移1，不是打包值数）
    body += bytes([0, 0])                            # 附加字节、解释（体偏移5,6）
    body += bytes([0])                               # 保留（体偏移7）
    body += struct.pack('>H', 0)                     # 模板号 3.0（体偏移8）
    template = bytearray(48)                         # 模板体自偏移 10 起
    # 模板 3.0 体（48 字节，体内偏移按 WMO 连续推导）：
    # 形状0 半径因子1 半径值2..5 Ni6..9 Nj10..13 基本角14..17 细分18..21
    # 首纬22..25 首经26..29 分辨率30 末纬31..34 末经35..38
    # i增量39..42 j增量43..46 扫描47
    template = bytearray(48)
    template[0] = 6                                  # 地球形状
    template[1] = 0
    struct.pack_into('>I', template, 2, 0)          # 半径缩放值
    struct.pack_into('>I', template, 6, ni)
    struct.pack_into('>I', template, 10, nj)
    struct.pack_into('>II', template, 14, 0, 0)     # 基本角/细分 0,0 => 1e-6 度
    struct.pack_into('>iI', template, 22,
                     _signed_magnitude(angle(first_lat), 32), angle(first_lon))
    template[30] = 0x30
    struct.pack_into('>iI', template, 31,
                     _signed_magnitude(angle(last_lat), 32), angle(last_lon))
    struct.pack_into('>ii', template, 39,
                     _signed_magnitude(angle(di), 32),
                     _signed_magnitude(angle(dj), 32))
    template[47] = scan_mode
    body += bytes(template)
    return section(3, bytes(body))


def product_section(parameter_category, parameter_number, time_unit,
                    forecast_time, first_surface_type=255,
                    process_type=0):
    """模板 4.0 确定性预报。"""

    body = bytearray()
    body += struct.pack('>H', 0)                     # 坐标值数
    body += struct.pack('>H', 0)                     # 模板号 4.0
    body += bytes(4)                                 # 保留
    template = bytearray(25)
    template[0] = parameter_category
    template[1] = parameter_number
    template[2] = process_type
    template[3] = 0
    template[4] = 0
    struct.pack_into('>H', template, 5, 0)           # 数据截止后小时
    template[7] = 0                                  # 分钟
    template[8] = time_unit
    struct.pack_into('>I', template, 9, forecast_time)
    template[13] = first_surface_type
    template[14] = 0
    struct.pack_into('>i', template, 15, 0)
    template[19] = 255
    template[20] = 0
    struct.pack_into('>i', template, 21, 0)
    body += bytes(template)
    return section(4, bytes(body))


def representation_section(number_values, reference_value, binary_scale,
                           decimal_scale, bits_per_value):
    """模板 5.0 simple packing。number_values 是打包值个数，可少于格点总数。"""

    body = bytearray()
    body += struct.pack('>I', number_values)
    body += struct.pack('>H', 0)                     # 模板号 5.0
    body += bytes(4)                                 # 保留
    template = bytearray(10)
    struct.pack_into('>f', template, 0, reference_value)
    struct.pack_into('>h', template, 4,
                     _signed_magnitude(binary_scale, 16))
    struct.pack_into('>h', template, 6,
                     _signed_magnitude(decimal_scale, 16))
    template[8] = bits_per_value
    template[9] = 0                                  # 现场数据：浮点
    body += bytes(template)
    return section(5, bytes(body))


def bitmap_section(validity_bits, indicator=0):
    """indicator=0 显式位图；255 全有效；254 复用先前位图。"""

    if indicator == 0:
        bit_count = len(validity_bits)
        padded = bytearray((bit_count + 7) // 8)
        for index, bit in enumerate(validity_bits):
            if bit:
                padded[index >> 3] |= 1 << (7 - (index & 7))
        return section(6, bytes([indicator]) + bytes(padded))
    return section(6, bytes([indicator]))


def data_section(values, reference_value, binary_scale, decimal_scale,
                 bits_per_value):
    """按 (Y*10^D - R)/2^E 反算整数 X，再以 MSB 位序打包。"""

    factor = (10.0 ** decimal_scale) / (2.0 ** binary_scale)
    packed = bytearray()
    accumulator = 0
    accumulated_bits = 0
    max_value = (1 << bits_per_value) - 1
    for value in values:
        integer = int(round((value * (10.0 ** decimal_scale)
                             - reference_value) / (2.0 ** binary_scale)))
        if integer < 0 or integer > max_value:
            raise ValueError('打包值超出 %s 位范围: %s' % (bits_per_value, integer))
        accumulator = (accumulator << bits_per_value) | integer
        accumulated_bits += bits_per_value
        while accumulated_bits >= 8:
            shift = accumulated_bits - 8
            packed.append((accumulator >> shift) & 0xFF)
            accumulator &= (1 << shift) - 1
            accumulated_bits -= 8
    if accumulated_bits:
        packed.append((accumulator << (8 - accumulated_bits)) & 0xFF)
    return section(7, bytes(packed))


def message(parts, discipline=0):
    body = b''.join(parts)
    header = b'GRIB' + bytes([0, 0, discipline, 2]) \
        + struct.pack('>Q', len(body) + 20)
    return header + body + b'7777'


def encode_field(grid, product, representation, bitmap, data,
                 include_grid=True, include_product=True,
                 include_representation=True):
    parts = []
    if include_grid:
        parts.append(grid)
    if include_product:
        parts.append(product)
    if include_representation:
        parts.append(representation)
    parts.extend([bitmap, data])
    return parts


# --------------------------------------------------------------------------
# 业务夹具
# --------------------------------------------------------------------------

def _logical_to_scan(ni, nj, scan_mode):
    """返回扫描序 k -> 逻辑稳定索引 (j*Ni+i) 的映射，用于把逻辑数据排成扫描序。

    与 collector._scan_order_xy 严格互逆：逻辑 j=0 在北。
    bit2=0 扫描首行在北(j=0)；bit2=1 扫描首行在南(j=Nj-1)。
    """

    i_negative = bool(scan_mode & 0x80)
    j_positive = bool(scan_mode & 0x40)
    mapping = []
    for k in range(ni * nj):
        i = (ni - 1 - (k % ni)) if i_negative else (k % ni)
        row = k // ni
        j = (nj - 1 - row) if j_positive else row
        mapping.append(j * ni + i)
    return mapping


def _wind_field(reference, forecast_hours, ni, nj, corners, scan_mode,
                validity, values, decimal_scale=0, bits_per_value=16,
                bitmap_indicator=0, include_grid=True, include_product=True,
                include_representation=True, reference_value=0.0,
                binary_scale=0, parameter_number=1):
    # validity 按逻辑稳定索引给出（1 有效 0 缺测）；values 只含逻辑有效点的值，
    # 按逻辑索引升序。这里按扫描模式重排成 GRIB 扫描序，确保正/反扫描表达同一网格。
    scan_map = _logical_to_scan(ni, nj, scan_mode)
    scan_validity = [validity[logical] for logical in scan_map]
    # 由逻辑有效点重建逻辑索引 -> 值，再按扫描序抽取
    logical_value_map = {}
    value_iter = iter(values)
    for logical in range(ni * nj):
        if validity[logical]:
            logical_value_map[logical] = next(value_iter)
    scan_values = [logical_value_map[logical] for logical in scan_map
                   if validity[logical]]

    first_lat, first_lon, last_lat, last_lon, di, dj = corners
    grid = grid_section(ni, nj, first_lat, first_lon, last_lat, last_lon,
                        di, dj, scan_mode)
    product = product_section(2, parameter_number, 1, forecast_hours)
    representation = representation_section(
        len(scan_values), reference_value, binary_scale, decimal_scale,
        bits_per_value)
    bitmap = bitmap_section(scan_validity, bitmap_indicator)
    data = data_section(scan_values, reference_value, binary_scale,
                        decimal_scale, bits_per_value)
    return encode_field(grid, product, representation, bitmap, data,
                        include_grid, include_product, include_representation)


def samples():
    from datetime import datetime

    fixtures = {}

    # ---- 历史夹具：3x2 网格、6 格点、5 打包值、1 个位图缺测（规范化重编码）----
    ref = datetime(2026, 11, 12, 0, 0, 0)
    validity = [1, 1, 1, 0, 1, 1]
    values = [10, 20, 30, 50, 60]                    # 缺测位置不占打包值
    parts = [identification_section(ref)]
    parts += _wind_field(ref, 6, 3, 2,
                         (1.0, 359.0, 0.0, 361.0, 1.0, 1.0), 0,
                         validity, values, decimal_scale=0)
    fixtures['sample.grib2'] = message(parts)
    fixtures['invalid.grib2'] = fixtures['sample.grib2'][:-1]

    # ---- 港区 4x3 网格（12 格点），跨零经线 359..362，正向扫描 ----
    harbor_corners = (31.0, 359.0, 29.0, 362.0, 1.0, 1.0)
    harbor_ni, harbor_nj = 4, 3
    harbor_validity = [1, 1, 0, 1,
                       1, 1, 1, 1,
                       0, 1, 1, 1]
    harbor_values = [31, 32, 34,
                     41, 42, 43, 44,
                     52, 53, 54]

    # 起报批次 A：2026-11-12 00Z，时效 6h，有效时刻 06Z
    ref_a = datetime(2026, 11, 12, 0, 0, 0)
    parts_a = [identification_section(ref_a)]
    parts_a += _wind_field(ref_a, 6, harbor_ni, harbor_nj, harbor_corners, 0,
                           harbor_validity, harbor_values)
    fixtures['harbor-batch-a.grib2'] = message(parts_a)

    # 起报批次 B：2026-11-11 18Z，时效 12h，有效时刻同为 2026-11-12 06Z
    ref_b = datetime(2026, 11, 11, 18, 0, 0)
    values_b = [v + 100 for v in harbor_values]
    parts_b = [identification_section(ref_b)]
    parts_b += _wind_field(ref_b, 12, harbor_ni, harbor_nj, harbor_corners, 0,
                           harbor_validity, values_b)
    fixtures['harbor-batch-b.grib2'] = message(parts_b)

    # ---- 反向扫描：i、j 方向均反向（扫描模式 0xC0），索引仍应稳定 ----
    # 同一物理网格（29..31N, 359..362E）改为反向扫描：扫描序首点落在原网格
    # 东南角 (29N,362E)，末点落在西北角 (31N,359E)，增量幅值不变。
    reverse_corners = (29.0, 362.0, 31.0, 359.0, 1.0, 1.0)
    ref_r = datetime(2026, 11, 12, 0, 0, 0)
    parts_r = [identification_section(ref_r)]
    parts_r += _wind_field(ref_r, 6, harbor_ni, harbor_nj, reverse_corners, 0xC0,
                           harbor_validity, harbor_values)
    fixtures['harbor-reverse-scan.grib2'] = message(parts_r)

    # ---- 同消息两场：第一场显式位图，第二场指示符 254 复用该位图 ----
    ref_u = datetime(2026, 11, 12, 0, 0, 0)
    first = _wind_field(ref_u, 6, harbor_ni, harbor_nj, harbor_corners, 0,
                        harbor_validity, harbor_values)
    second_values = [v + 200 for v in harbor_values]
    second = _wind_field(ref_u, 12, harbor_ni, harbor_nj, harbor_corners, 0,
                         harbor_validity, second_values,
                         bitmap_indicator=254)
    fixtures['harbor-reused-bitmap.grib2'] = message(
        [identification_section(ref_u)] + first + second)

    # ---- 坏场：指示符 254 复用位图但同消息无先前位图；后面跟一条完好消息 ----
    ref_bad = datetime(2026, 11, 12, 0, 0, 0)
    orphan = _wind_field(ref_bad, 6, harbor_ni, harbor_nj, harbor_corners, 0,
                         harbor_validity, harbor_values, bitmap_indicator=254)
    bad_message = message([identification_section(ref_bad)] + orphan)
    good_message = fixtures['harbor-batch-a.grib2']
    fixtures['harbor-orphan-bitmap.grib2'] = bad_message + good_message

    # ---- 不支持的网格模板（模板号写成 3.1），该场拒绝但消息可继续检查 ----
    bad_grid = bytearray(grid_section(harbor_ni, harbor_nj, *harbor_corners,
                                      scan_mode=0))
    # 段体中模板号位于段长(4)+段号(1)+体偏移8
    struct.pack_into('>H', bad_grid, 5 + 8, 1)
    ref_t = datetime(2026, 11, 12, 0, 0, 0)
    representation = representation_section(
        sum(harbor_validity), 0.0, 0, 0, 16)
    bitmap = bitmap_section(harbor_validity, 0)
    data = data_section(harbor_values, 0.0, 0, 0, 16)
    fixtures['harbor-unsupported-template.grib2'] = message(
        [identification_section(ref_t), bytes(bad_grid),
         product_section(2, 1, 1, 6), representation, bitmap, data])

    return fixtures
