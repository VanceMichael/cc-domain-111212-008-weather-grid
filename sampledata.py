import struct

def section(number, body):
    return struct.pack('>IB', len(body) + 5, number) + body

def samples():
    identification = struct.pack('>HHBBBHBBBBBBB', 0, 0, 2, 0, 1, 2026, 11, 12, 0, 0, 0, 0, 1)
    grid = bytearray(67)
    struct.pack_into('>BIBBH', grid, 0, 0, 6, 0, 0, 0)
    grid[9] = 6
    struct.pack_into('>II', grid, 25, 3, 2)
    struct.pack_into('>II', grid, 33, 0, 0)
    struct.pack_into('>ii', grid, 41, 1000000, 359000000)
    grid[49] = 48
    struct.pack_into('>iiII', grid, 50, 0, 1000000, 1000000, 1000000)
    grid[66] = 0
    product = bytearray(29)
    struct.pack_into('>HH', product, 0, 0, 0)
    product[4:6] = bytes([2, 2])
    product[12] = 1
    struct.pack_into('>I', product, 13, 6)
    product[17] = 103
    product[23] = 255
    representation = struct.pack('>IHfhhBB', 5, 0, 0.0, 0, 0, 8, 0)
    body = section(1, identification) + section(3, grid) + section(4, product)
    body += section(5, representation) + section(6, bytes([0, 0b11101100]))
    body += section(7, bytes([1, 2, 3, 4, 5]))
    message = b'GRIB' + bytes([0, 0, 0, 2]) + struct.pack('>Q', len(body) + 20) + body + b'7777'
    return {'sample.grib2': message, 'invalid.grib2': message[:-1]}
