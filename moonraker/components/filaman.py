# Filament manager changing flow-rate, pressure advance, etc. per filament
#
# Copyright (C) 2024 Lukas Hruska <lukyhruska96@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from smbus2 import SMBus
import struct
from enum import Enum
from ..confighelper import ConfigHelper

class BusType(Enum):
    BOOL = 0
    INT = 1
    FLOAT = 2
    STR = 3
    BYTE = 4

class BusOp(Enum):
    COMMIT = 0

BUS_REGISTERS = {
    'operation': { 'reg': 0x0F, 'type': BusType.BYTE },
    'present': { 'reg': 0x00, 'type': BusType.BOOL },
    'nfc_valid': { 'reg': 0x01, 'type': BusType.BOOL },
    'filament_id': { 'reg': 0x02, 'type': BusType.INT },
    'filament_name_len': { 'reg': 0x03, 'type': BusType.INT },
    'filament_name': { 'reg': 0x04, 'type': BusType.STR },
    'spool_weight': { 'reg': 0x05, 'type': BusType.INT },
    'total_weight': { 'reg': 0x06, 'type': BusType.INT },
    'opt_bitmap': { 'reg': 0x07, 'type': BusType.BYTE },
    'extr_multiplier': { 'reg': 0x08, 'type': BusType.FLOAT },
    'pressure_advance': { 'reg': 0x09, 'type': BusType.FLOAT },
    'extr_temp': { 'reg': 0x0A, 'type': BusType.INT },
    'bed_temp': { 'reg': 0x0B, 'type': BusType.INT },
    'regular_fan': { 'reg': 0x0C, 'type': BusType.INT },
    'bridge_fan': { 'reg': 0x0D, 'type': BusType.INT }
}

BUS_REG_OPT_START = BUS_REGISTERS['opt_bitmap']['reg']+1

FILAMENT_CHANGED_NOTIF = "filaman:filament_changed"

class FilaMan:
    def __init__(self, config: ConfigHelper):
        self.server = config.get_server()
        self.name = config.get_name()
        self.config = config

        self.status = {}

        # Optional config
        self.smbus_id = config.getint("smbus_id", 0)

        # Required config
        self.dev_addr = config.getint("dev_address")
        config.getgpioevent("int_pin", self._on_int_event)

        self._register_i2c()
        self._register_endpoints()

        self.server.register_notification(FILAMENT_CHANGED_NOTIF)

    def _check_i2c_dev(self, addr):
        try:
            self.bus.read_byte(addr)
        except:
            raise self.server.error("There is no device on i2c bus with address {}".format(addr))

    def _register_i2c(self):
        self.bus = SMBus(self.smbus_id)
        self._check_i2c_dev(self.dev_addr)

    def _register_endpoints(self):
        self.server.register_endpoint("/server/filaman/status", ['GET'],
                                      self._handle_status)
        self.server.register_endpoint("/server/filaman/nfc_read", ['GET'],
                                      self._handle_nfc_read)
        self.server.register_endpoint("/server/filaman/nfc_write", ['POST'],
                                      self._handle_nfc_write)

    async def _handle_status(self, web_request):
        return self._read_status()

    async def _handle_nfc_read(self, web_request):
        return self._read_nfc()

    async def _handle_nfc_write(self, web_request):
        data = web_request.get_args()
        self._write_nfc(data)
        return {"status": "OK"}

    def _notify_filament_changed(self):
        filament_info = self.status
        if filament_info['present'] and filament_info['nfc_valid']:
            filament_info = dict(filament_info, **self._read_nfc())
        self.server.send_event(FILAMENT_CHANGED_NOTIF, filament_info)

    async def _on_int_event(
        self, eventtime: float, elapsed_time: float, pressed: int
        ):
        self._notify_filament_changed()

    def _read_status(self):
        self.status['present'] = self._read_register(BUS_REGISTERS['present'])
        self.status['nfc_valid'] = self._read_register(BUS_REGISTERS['nfc_valid'])
        return self.status

    def _get_bit(self, value, n):
        return ((value >> n & 0x01) != 0x00)

    def _set_bit(self, value, n):
        return (value | (1 << n))

    def _get_bitmap_val(self, value, identifier):
        return self._get_bit(value, identifier - BUS_REG_OPT_START)

    def _read_nfc(self):
        data = {}
        name_len = self._read_register(BUS_REGISTERS['filament_name_len'])
        data['id'] = self._read_register(BUS_REGISTERS['filament_id'])
        print("Filament name len: {}".format(name_len))
        data['name'] = self._read_str(BUS_REGISTERS['filament_name']['reg'], name_len)
        data['spool_weight'] = self._read_register(BUS_REGISTERS['spool_weight'])
        data['total_weight'] = self._read_register(BUS_REGISTERS['total_weight'])
        opt_bitmap = self._read_register(BUS_REGISTERS['opt_bitmap'])
        for key, val in BUS_REGISTERS.items():
            if val['reg'] < BUS_REG_OPT_START:
                continue
            if (self._get_bitmap_val(opt_bitmap, val['reg'])):
                data[key] = self._read_register(val)
        return data

    def _write_nfc(self, data):
        self._write_register(BUS_REGISTERS['filament_id'], data['id'])
        self._write_str(
            BUS_REGISTERS['filament_name']['reg'],
            data['name'],
            BUS_REGISTERS['filament_name_len']['reg'])
        self._write_register(BUS_REGISTERS['spool_weight'], data['spool_weight'])
        self._write_register(BUS_REGISTERS['total_weight'], data['total_weight'])
        opt_bitmap = 0x00
        for key, val in BUS_REGISTERS.items():
            if val['reg'] < BUS_REG_OPT_START:
                continue
            if key in data:
                opt_bitmap = self._set_bit(opt_bitmap, val['reg'] - BUS_REG_OPT_START)
                self._write_register(val, data[key])
        self._write_register(BUS_REGISTERS['opt_bitmap'], opt_bitmap)
        self._write_operation(BusOp.COMMIT)

    def _read_float(self, register):
        buf = self.bus.read_i2c_block_data(self.dev_addr, register, 4)
        return struct.unpack('<f', bytearray(buf))[0]

    def _write_float(self, register, value):
        buf = struct.pack('<f', value)
        self.bus.write_i2c_block_data(self.dev_addr, register, buf)

    def _read_int(self, register):
        buf = self.bus.read_i2c_block_data(self.dev_addr, register, 4)
        return struct.unpack('<i', bytearray(buf))[0]

    def _write_int(self, register, value):
        buf = struct.pack('<i', value)
        self.bus.write_i2c_block_data(self.dev_addr, register, buf)

    def _read_bool(self, register):
        val = self.bus.read_byte_data(self.dev_addr, register)
        return val != 0

    def _write_bool(self, register, value):
        self.bus.write_byte_data(self.dev_addr, register, 1 if value else 0)

    def _read_byte(self, register):
        return self.bus.read_byte_data(self.dev_addr, register)

    def _write_byte(self, register, value):
        self.bus.write_byte_data(self.dev_addr, register, value)

    def _read_str(self, register, length):
        buf = self.bus.read_i2c_block_data(self.dev_addr, register, min(32, length))
        return bytes(buf).decode('utf-8')

    def _write_str(self, register, value, length_register):
        value = value[:32] if len(value) >= 32 else value

        length = len(value)
        self._write_int(length_register, length)

        self.bus.write_i2c_block_data(self.dev_addr, register, value.encode('utf-8'))

    def _read_register(self, reg_val):
        if reg_val['type'] == BusType.INT:
            return self._read_int(reg_val['reg'])
        elif reg_val['type'] == BusType.FLOAT:
            return self._read_float(reg_val['reg'])
        elif reg_val['type'] == BusType.BOOL:
            return self._read_bool(reg_val['reg'])
        elif reg_val['type'] == BusType.BYTE:
            return self._read_byte(reg_val['reg'])
        else:
            raise self.system.error("Optional field cannot contain string value")

    def _write_register(self, reg_val, val):
        if reg_val['type'] == BusType.INT:
            self._write_int(reg_val['reg'], val)
        elif reg_val['type'] == BusType.FLOAT:
            self._write_float(reg_val['reg'], val)
        elif reg_val['type'] == BusType.BOOL:
            return self._write_bool(reg_val['reg'], val)
        elif reg_val['type'] == BusType.BYTE:
            return self._write_byte(reg_val['reg'], val)
        else:
            raise self.system.error("Optional field cannot contain string value")

    def _write_operation(self, op):
        self._write_byte(BUS_REGISTERS['operation']['reg'], op.value)

def load_component(config):
    return FilaMan(config)
