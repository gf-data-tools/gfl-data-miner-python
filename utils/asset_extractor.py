import json
import os
import sys
import logging
import UnityPy


def unpack_all_assets(file: str, destination_folder: str):
    logging.info(f'unpacking {file}')
    env = UnityPy.load(file)
    logging.info(env.container)
    for path, obj in env.container.items():
        data = obj.read()
        out = None
        logging.info(f'processing {obj.type.name} {path}')
        if obj.type.name in ["TextAsset"]:
            out = data.script
        elif obj.type.name in ["MonoBehaviour"]:
            if obj.serialized_type.nodes:
                out = json.dumps(obj.read_typetree(), indent=4, ensure_ascii=False).encode("utf8")
            else:
                out = data.raw_data
        else:
            continue
        dest = os.path.join(destination_folder, *path.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(bytes(out))


if __name__ == "__main__":
    unpack_all_assets(sys.argv[1], sys.argv[2])
