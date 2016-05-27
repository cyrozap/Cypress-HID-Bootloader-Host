#!/usr/bin/env python3
#
# Copyright (C) 2016  Forest Crossman <cyrozap@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import argparse
import binascii
import sys

import hid


class Bootloader():
    MAX_DATA_LENGTH = 64-7-9

    STATUSES = {
        0x00: "CYRET_SUCCESS",
        0x03: "BOOTLOADER_ERR_LENGTH",
        0x04: "BOOTLOADER_ERR_DATA",
        0x05: "BOOTLOADER_ERR_CMD",
        0x08: "BOOTLOADER_ERR_CHECKSUM",
        0x09: "BOOTLOADER_ERR_ARRAY",
        0x0a: "BOOTLOADER_ERR_ROW",
        0x0c: "BOOTLOADER_ERR_APP",
        0x0d: "BOOTLOADER_ERR_ACTIVE",
        0x0e: "BOOTLOADER_ERR_CALLBACK",
        0x0f: "BOOTLOADER_ERR_UNK"
    }

    def __init__(self, vid=0, pid=0, serial=None):
        try:
            self._device = hid.device()
            self._device.open(vid, pid)
        except OSError as error:
            sys.stderr.write("Error: {}".format(error))

        self.jtag_id = None
        self.device_revision = None
        self.bootloader_revision = None

    def _checksum(self, data):
        checksum = 0
        for byte in data:
            checksum += byte
        checksum = (~checksum + 1) & 0xffff
        return checksum

    def _make_packet(self, command, data=[]):
        packet = []
        packet.append(0x01)

        packet.append(command)

        data_length = len(data)
        assert data_length <= (64 - 7)
        packet.append(data_length & 0xff)
        packet.append(data_length >> 8)

        for byte in data:
            packet.append(byte)

        checksum = self._checksum(packet)
        packet.append(checksum & 0xff)
        packet.append(checksum >> 8)

        packet.append(0x17)
        return packet

    def _parse_response(self, response_data):
        response = {
            "status": "BOOTLOADER_ERR_UNK",
            "data": [],
            "checksum_ok": False,
        }

        sop = response_data[0]
        assert sop == 0x01

        response["status"] = self.STATUSES[response_data[1]]

        data_length = (response_data[3] << 8) | response_data[2]

        if data_length > 0:
            response["data"] = response_data[4:4+data_length]

        checksum_received = response_data[4+data_length]
        checksum_received |= response_data[4+data_length+1] << 8
        checksum_calculated = self._checksum(response_data[:4+data_length])
        response["checksum_ok"] = (checksum_calculated == checksum_received)

        eop = response_data[4+data_length+2]
        assert eop == 0x17

        return response

    def send_command(self, command, data=[]):
        packet = self._make_packet(command, data)
        self._device.write(packet)

        response_data = self._device.read(64)
        response = self._parse_response(response_data)
        return response

    def enter_bootloader(self):
        response = self.send_command(0x38)
        if response["checksum_ok"] and response["status"] == "CYRET_SUCCESS":
            self.jtag_id = response["data"][0]
            self.jtag_id |= response["data"][1] << 8
            self.jtag_id |= response["data"][2] << 16
            self.jtag_id |= response["data"][3] << 24
            self.device_revision = response["data"][4]
            self.bootloader_revision = response["data"][5:8][::-1]
            return True
        else:
            return False

    def program_row(self, array_id, row_number, data):
        arguments = [array_id, (row_number & 0xff), (row_number >> 8)]
        response = self.send_command(0x39, arguments + data)
        if response["checksum_ok"] and response["status"] == "CYRET_SUCCESS":
            return True
        else:
            return False

    def erase_row(self, array_id, row_number):
        arguments = [array_id, (row_number & 0xff), (row_number >> 8)]
        response = self.send_command(0x34, arguments)
        if response["checksum_ok"] and response["status"] == "CYRET_SUCCESS":
            return True
        else:
            return False

    def send_data(self, data):
        response = self.send_command(0x37, data)
        if response["checksum_ok"] and response["status"] == "CYRET_SUCCESS":
            return True
        else:
            return False

    def exit_bootloader(self):
        packet = self._make_packet(0x3b, [])
        self._device.write(packet)

    def flash(self, firmware):
        firmware_data = firmware.firmware
        for row in firmware_data:
            array_id = row[0]
            row_number = row[1]
            data = list(row[2])
            if len(data) >= self.MAX_DATA_LENGTH:
                full_packets = len(data)//self.MAX_DATA_LENGTH
                partial_packet_length = len(data)%self.MAX_DATA_LENGTH
                if partial_packet_length > 0:
                    for i in range(0, full_packets):
                        self.send_data(data[self.MAX_DATA_LENGTH*i:self.MAX_DATA_LENGTH*i+self.MAX_DATA_LENGTH])
                    self.program_row(array_id, row_number, data[self.MAX_DATA_LENGTH*full_packets:self.MAX_DATA_LENGTH*full_packets+partial_packet_length])
                else:
                    for i in range(0, full_packets-1):
                        self.send_data(data[self.MAX_DATA_LENGTH*i:self.MAX_DATA_LENGTH*i+self.MAX_DATA_LENGTH])
                    self.program_row(array_id, row_number, data[self.MAX_DATA_LENGTH*(full_packets-1):self.MAX_DATA_LENGTH*(full_packets-1)+self.MAX_DATA_LENGTH])
            else:
                self.program_row(array_id, row_number, data)

class Cyacd():
    def __init__(self, firmware_file):
        self.file = firmware_file
        self.silicon_id = None
        self.silicon_revision = None
        self.checksum_type = None
        self.firmware = None

    def _checksum(self, data):
        checksum = 0
        for byte in data:
            checksum += byte
        checksum = (~checksum + 1) & 0xff
        return checksum

    def parse(self):
        lines = self.file.readlines()

        header_line = lines.pop(0).rstrip('\r\n')
        header = binascii.a2b_hex(header_line)
        self.silicon_id = header[3]
        self.silicon_id |= header[2] << 8
        self.silicon_id |= header[1] << 16
        self.silicon_id |= header[0] << 24
        self.silicon_revision = header[4]
        self.checksum_type = header[5]

        firmware = []
        for line in lines:
            line = binascii.a2b_hex(line[1:].rstrip('\r\n'))
            array_id = line[0]
            row_number = (line[1] << 8) | line[2]
            data_length = (line[3] << 8) | line[4]
            data = line[5:5+data_length]
            checksum = line[5+data_length]
            checksum_calculated = self._checksum(line[:-1])
            assert checksum == checksum_calculated
            firmware.append((array_id, row_number, data))

        self.firmware = firmware

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vid_pid_string", type=str)
    parser.add_argument("firmware_file", type=str)
    args = vars(parser.parse_args())

    vid_pid_string = args["vid_pid_string"].split(':')

    vid = int(vid_pid_string[0], 16)
    pid = int(vid_pid_string[1], 16)

    found = False
    for enumerated in hid.enumerate():
        if enumerated["vendor_id"] == vid and enumerated["product_id"] == pid:
            found = True
            break

    if found:
        bootloader = Bootloader(vid, pid)
        bootloader.enter_bootloader()

        firmware = Cyacd(open(args["firmware_file"], 'r'))
        firmware.parse()

        if (bootloader.jtag_id == firmware.silicon_id) and (bootloader.device_revision == firmware.silicon_revision):
            bootloader.flash(firmware)

        bootloader.exit_bootloader()
    else:
        sys.stderr.write("Device not found!\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
