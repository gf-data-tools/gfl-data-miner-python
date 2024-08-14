# %%
import json
import logging
import os
import struct
from collections import OrderedDict


# %%
class StcReader:
    def __init__(self, stc):
        self.file = open(stc, "rb")

    def __del__(self):
        self.file.close()

    def read_byte(self):
        b = self.file.read(1)
        return struct.unpack("<b", b)[0]

    def read_ushort(self):
        b = self.file.read(2)
        return struct.unpack("<H", b)[0]

    def read_int(self):
        b = self.file.read(4)
        return struct.unpack("<i", b)[0]

    def read_long(self):
        b = self.file.read(8)
        return struct.unpack("<q", b)[0]

    def read_float(self):
        b = self.file.read(4)
        f32 = struct.unpack("<f", b)[0]
        return float("%g" % f32)

    def read_str(self):
        self.skip_bytes(1)
        length = self.read_ushort()
        b = self.file.read(length)
        return struct.unpack(f"{length}s", b)[0].decode("utf-8")

    def skip_bytes(self, n):
        self.file.read(n)

    def seek(self, offset):
        self.file.seek(offset)

    def read(self, id):
        return {
            1: self.read_byte,
            5: self.read_int,
            8: self.read_long,
            9: self.read_float,
            11: self.read_str,
        }[id]()


def format_stc(stc: str, mapping: str, long=False):
    with open(mapping, "r") as f:
        stc_conf = json.load(f)

    reader = StcReader(stc)
    code = reader.read_ushort()
    if not long:
        reader.skip_bytes(2)
    else:
        reader.skip_bytes(4)
    logging.debug(f"reading {os.path.split(stc)[-1]}, code {code}")
    data = list()
    if not long:
        row = reader.read_ushort()
    else:
        row = reader.read_int()
    if row == 0:
        return stc_conf["name"], data
    col = reader.read_byte()
    logging.debug(f"col {col}, row {row}")

    type_ids = []
    for _ in range(col):
        type_ids.append(reader.read_byte())
    if len(type_ids) < len(stc_conf["fields"]):
        logging.warning(f"redundant field in {os.path.split(stc)[-1]}, code {code}")
        stc_conf["fields"] = stc_conf["fields"][: len(type_ids)]
    if len(type_ids) > len(stc_conf["fields"]):
        logging.warning(f"unknown field in {os.path.split(stc)[-1]}, code {code}")
        for i in range(len(stc_conf["fields"]), len(type_ids)):
            stc_conf["fields"].append(f"unk_{i}")

    format = {
        stc_conf["fields"][i]: {
            1: "byte",
            5: "int",
            8: "long",
            9: "float",
            11: "string",
        }[type_ids[i]]
        for i in range(col)
    }
    logging.debug(f'{stc_conf["name"]}: {format}')

    reader.skip_bytes(4)
    offset = reader.read_int()
    reader.seek(offset)
    for _ in range(row):
        record = OrderedDict()
        for key, id in zip(stc_conf["fields"], type_ids):
            record[key] = reader.read(id)
        logging.debug(record)
        data.append(record)
    return stc_conf["name"], data


# %%
if __name__ == "__main__":
    logging.basicConfig(level="DEBUG")
    stc = r"D:\Workspace\gfline\GF_Data_Tools\data-miner\raw\at\stc\5000.stc"
    mapping = (
        r"D:\Workspace\gfline\GF_Data_Tools\data-miner\conf\stc-mapping\3020\5000.json"
    )
    table = format_stc(stc, mapping, long=True)
    print(table)


# %%
