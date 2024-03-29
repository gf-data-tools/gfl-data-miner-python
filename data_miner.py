# %%
import argparse
import base64
import io
import json
import logging
import os
import re
import shutil
import sys
import traceback
from gzip import GzipFile
from pathlib import Path
from urllib import request
from zipfile import ZipFile

import hjson
import pandas as pd
import pyjson5
from compact_json.formatter import EolStyle, Formatter
from gf_utils.stc_data import get_stc_data
from git import Git
from git.repo import Repo

from utils.asset_extractor import unpack_all_assets
from utils.crypto import get_des_encrypted, get_md5_hash, xor_decrypt
from utils.download import download
from utils.format_stc import format_stc

os.chdir(Path(__file__).resolve().parent)

CONFIG_JSON5 = r"conf/config.json5"
with open(CONFIG_JSON5, "r", encoding="utf-8") as f:
    conf = pyjson5.load(f)
RAW_ROOT = r"raw"

DATA_ROOT = conf["git"]["local"]
PERSONAL_TOKEN = os.environ.get("PERSONAL_TOKEN", None)
AUTHOR = os.environ.get("AUTHOR", "Author <>")
DINGTALK_TOKEN = os.environ.get("DINGTALK_TOKEN", "")


class GithubEnv:
    def __init__(self):
        self.file = Path(os.environ.get("GITHUB_ENV", "env.txt"))

    def __setitem__(self, name, value):
        with self.file.open("a") as f:
            f.write(f"{name}={value}\n")


# %%
class DataMiner:
    def __init__(self, region="ch"):
        self.region = region
        self.raw_dir = os.path.join(RAW_ROOT, region)
        self.data_dir = os.path.join(DATA_ROOT, region)
        self.host = pyjson5.loads(self.read_repo_file("hosts.json5"))
        self.res_key = conf["res_key"]
        self.res_iv = conf["res_iv"]
        self.lua_key = conf["lua_key"]
        self.dat_key = conf["dat_key"]
        self.github_env = GithubEnv()
        self.json_formatter = Formatter(
            max_inline_length=120,
            max_inline_complexity=1 << 31,
            max_compact_list_complexity=1 << 31,
            indent_spaces=2,
            json_eol_style=EolStyle.LF,
            ensure_ascii=False,
            east_asian_string_widths=True,
            multiline_compact_dict=True,
        )

    def read_repo_file(self, filepath):
        req = request.Request(
            f"https://raw.githubusercontent.com/gf-data-tools/"
            f"gf-data-{self.region}/main/{filepath}",
            headers={
                "Authorization": f"Bearer {PERSONAL_TOKEN}" if PERSONAL_TOKEN else ""
            },
        )
        return request.urlopen(req).read().decode("utf-8")

    @property
    def git_url(self):
        if not PERSONAL_TOKEN:
            return f"https://github.com/gf-data-tools/gf-data-{self.region}.git"
        else:
            return (
                f"https://oauth2:{PERSONAL_TOKEN}@github.com/"
                f"gf-data-tools/gf-data-{self.region}.git"
            )

    def get_current_version(self):
        logging.info(f"Requesting version")
        version_url = self.host["game_host"] + "/Index/version"
        response = request.urlopen(version_url).read().decode()
        logging.info(f"Response: {response}")
        version = pyjson5.loads(response)
        self.version = {k: v for k, v in version.items() if "version" in k}
        self.dataVersion = version["data_version"]
        self.clientVersion = version["client_version"]
        if len(self.clientVersion) == 5:
            self.minversion = round(eval(self.clientVersion) / 100) * 10
        else:
            self.minversion = eval(self.clientVersion)
        if self.clientVersion == "29999":
            self.minversion = 3010
        if self.clientVersion == "3010":
            self.minversion = 3020
        if self.clientVersion == "30100" and self.region == "ch":
            self.minversion = 3020
        self.abVersion = version["ab_version"]

    def get_res_data(self):
        logging.info(f"Getting resource data list")
        bkey = base64.standard_b64decode(self.res_key)
        biv = base64.standard_b64decode(self.res_iv)
        if self.region == "at":
            fname = f"{self.minversion}_alpha2020_{self.abVersion}_AndroidResConfigData"
        else:
            fname = f"{self.minversion}_{self.abVersion}_AndroidResConfigData"
        logging.debug(f"resdata name {fname}")
        en = get_des_encrypted(fname, bkey, biv[:8])
        res_config = base64.standard_b64encode(en).decode("utf-8")
        res_config = re.sub(r"[^a-zA-Z0-9]", "", res_config) + ".txt"
        resdata_url = self.host["asset_host"] + "/" + res_config
        resdata_fp = os.path.join(self.raw_dir, "AndroidResConfigData")
        download(resdata_url, resdata_fp)
        unpack_all_assets(resdata_fp, self.raw_dir)
        with open(
            os.path.join(self.raw_dir, "assets/resources/resdata.asset"),
            encoding="utf-8",
        ) as f:
            self.resdata = pyjson5.load(f)
        self.daBaoTime = self.resdata["daBaoTime"]
        self.version["dabao_time"] = self.resdata["daBaoTime"]

    def process_resdata(self):
        logging.info("Processing resdata")
        # shutil.copy(
        #     os.path.join(self.raw_dir, "assets/resources/resdata.asset"),
        #     os.path.join(self.data_dir, "resdata.json"),
        # )
        for k in ["passivityAssetBundles", "BaseAssetBundles", "AddAssetBundles"]:
            self.resdata[k].sort(key=lambda x: x["assetBundleName"])
            for r in self.resdata[k]:
                r["assetAllRes"].sort(key=lambda x: x["pathKey"])
                for a in r["assetAllRes"]:
                    a.pop("hashCode", None)
                    a.pop("hasCodes", None)
        with open(
            os.path.join(self.data_dir, "resdata_no_hash.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(self.resdata, f, indent=4, ensure_ascii=False)

    def get_asset_bundles(self):
        res_url = self.resdata["resUrl"]
        targets = [
            "asset_textavg",
            "asset_texttable",
            "asset_textes",
            "asset_textlangue",
            "asset_assetother",
        ]
        for ab_info in self.resdata["BaseAssetBundles"]:
            if ab_info["assetBundleName"] in targets:
                ab_url = f'{res_url}{ab_info["resname"]}.ab'
                ab_fp = os.path.join(self.raw_dir, f'{ab_info["assetBundleName"]}.ab')
                download(ab_url, ab_fp)

    def get_stc(self):
        hash = get_md5_hash(self.dataVersion)
        stc_url = (
            self.host["cdn_host"] + "/data/stc_" + self.dataVersion + hash + ".zip"
        )
        stc_fp = os.path.join(self.raw_dir, "stc.zip")
        download(stc_url, stc_fp)
        ZipFile(stc_fp).extractall(os.path.join(self.raw_dir, "stc"))

    @property
    def version_str(self):
        return f"[{self.region.upper()}] {self.clientVersion} | data {self.dataVersion[:7]} | dabao {self.daBaoTime[:-32]}"

    def update_raw_resource(self, force=False, extract_only=False):
        if os.path.exists(self.raw_dir):
            shutil.rmtree(self.raw_dir)
        if os.path.exists(self.data_dir):
            shutil.rmtree(self.data_dir)
        os.makedirs(self.raw_dir, exist_ok=True)
        self.get_current_version()
        self.get_res_data()
        logging.info(self.version_str)

        available = False
        if not force:
            version = pyjson5.loads(self.read_repo_file("version.json"))
            if version["data_version"] != self.dataVersion:
                available = True
            resdata = pyjson5.loads(self.read_repo_file("resdata_no_hash.json"))
            if resdata["daBaoTime"] != self.daBaoTime:
                available = True
        else:
            available = True

        if not available:
            logging.info("current data is up to date")
            return False

        logging.info("New data available")
        logging.info("Initializing Repo")
        if Path(self.data_dir).exists():
            repo = Repo(self.data_dir)
        else:
            repo = Repo.clone_from(self.git_url, to_path=self.data_dir, depth=1)
        self.remove_old_data()
        self.get_asset_bundles()
        self.get_stc()
        self.process_resdata()
        self.process_assets()
        self.process_catchdata()
        self.process_stc()
        self.format_data()
        with open(
            os.path.join(self.data_dir, "version.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(self.version, f, indent=4, ensure_ascii=False)

        repo.git.add(all=True)
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "github-actions[bot]")
            cw.set_value("user", "email", "<>")
            cw.set_value("core", "autocrlf", "input")
        repo.git.commit(
            m=self.version_str, author="ZeroRin <ZeroRin@users.noreply.github.com>"
        )
        repo.remote().set_url(self.git_url)
        repo.remote().push()
        self.dingtalk_notice(self.version_str)
        return True

    def remove_old_data(self):
        for content in Path(self.data_dir).iterdir():
            if content.is_dir() and content.name != ".git":
                shutil.rmtree(content)

    def process_assets(self):
        logging.info("Processing assets")
        for asset in [
            "asset_textavg",
            "asset_texttable",
            "asset_textes",
            "asset_textlangue",
            "asset_assetother",
        ]:
            if os.path.exists(os.path.join(self.raw_dir, asset + ".ab")):
                unpack_all_assets(
                    os.path.join(self.raw_dir, asset + ".ab"), self.raw_dir
                )
        for f in Path(self.raw_dir).glob("**/*.asset"):
            os.remove(f)
        asset_output = os.path.join(self.data_dir, "asset")
        os.makedirs(asset_output, exist_ok=True)
        asset_dir = os.path.join(self.raw_dir, "assets/resources/dabao")
        for subdir in os.listdir(asset_dir):
            shutil.copytree(
                os.path.join(asset_dir, subdir),
                os.path.join(asset_output, subdir),
                dirs_exist_ok=True,
            )
        shutil.copytree(
            os.path.join(self.raw_dir, "assets/resources/textdata/language"),
            os.path.join(asset_output, "language"),
            dirs_exist_ok=True,
        )
        self.decode_luapatch()

    def decode_luapatch(self):
        logging.info("Decrypting lua")
        src_dir = os.path.join(self.data_dir, "asset/luapatch")
        for root, dirs, files in os.walk(src_dir):
            for file in files:
                if not file.endswith("txt"):
                    continue
                logging.debug(f"decoding {file}")
                with open(os.path.join(root, file), "rb") as f:
                    cipher = f.read()
                plain = xor_decrypt(cipher, self.lua_key)
                with open(os.path.join(root, file[:-4]), "wb") as f:
                    f.write(plain)
                os.remove(os.path.join(root, file))

    def process_catchdata(self):
        logging.info(f"Decoding catchdata")
        dst_dir = os.path.join(self.data_dir, "catchdata")
        os.makedirs(dst_dir, exist_ok=True)
        with open(os.path.join(self.raw_dir, "stc/catchdata.dat"), "rb") as f:
            cipher = f.read()
        compressed = xor_decrypt(cipher, self.dat_key)
        plain = GzipFile(fileobj=io.BytesIO(compressed)).read().decode("utf-8")
        with open(os.path.join(dst_dir, "catchdata"), "w", encoding="utf-8") as f:
            f.write(plain)
        logging.info(f"Extracting json from catchdata")
        for json_string in plain.split("\n")[:-1]:
            data = json.loads(json_string)
            assert len(data.keys()) == 1
            for key in data.keys():
                logging.debug(f"Formatting {key}.json")
                # (Path(dst_dir) / f"{key}.json").write_text(self.json_formatter.serialize(data[key]))
                with open(
                    os.path.join(dst_dir, f"{key}.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(data[key], f, indent=4, ensure_ascii=False)

    def process_stc(self):
        mapping_dir = os.path.join("conf/stc-mapping", str(self.minversion))
        logging.info(f"Reading stc-mapping from {mapping_dir}")
        stc_dir = os.path.join(self.raw_dir, "stc")
        dst_dir = os.path.join(self.data_dir, "stc")
        os.makedirs(dst_dir, exist_ok=True)

        for f in os.listdir(stc_dir):
            logging.info(f"Formating {f}")
            id, ext = os.path.splitext(f)
            if ext != ".stc":
                continue
            stc = os.path.join(stc_dir, f"{id}.stc")
            mapping = os.path.join(mapping_dir, f"{id}.json")
            name, data = format_stc(stc, mapping, self.minversion == 3020)
            # (Path(dst_dir) / f"{name}.json").write_text(self.json_formatter.serialize(data))
            with open(
                os.path.join(dst_dir, f"{name}.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

    def format_data(self):
        logging.info("Formatting json and hjson outputs")
        json_output_dir = os.path.join(self.data_dir, "formatted", "json")
        hjson_output_dir = os.path.join(self.data_dir, "formatted", "hjson")
        os.makedirs(json_output_dir, exist_ok=True)
        os.makedirs(hjson_output_dir, exist_ok=True)
        table_dir = os.path.join(self.data_dir, "asset/table")
        for j in ["catchdata", "stc"]:
            json_dir = os.path.join(self.data_dir, j)
            data = get_stc_data(json_dir, table_dir, to_dict=False)
            for key, value in data.items():
                (Path(json_output_dir) / f"{key}.json").write_text(
                    self.json_formatter.serialize(value)
                )
                # with open(os.path.join(json_output_dir,f'{key}.json'),'w',encoding='utf-8') as f:
                #     json.dump(value,f,ensure_ascii=False,indent=4)
                for record in value:
                    for k, v in dict(record).items():
                        if v == "" or v == "0" or v == 0:
                            record.pop(k)
                with open(
                    os.path.join(hjson_output_dir, f"{key}.hjson"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    hjson.dump(value, f)

    def dingtalk_notice(self, message: str):
        url = f"https://oapi.dingtalk.com/robot/send?access_token={DINGTALK_TOKEN}"
        header = {"Content-Type": "application/json"}
        data = json.dumps(
            dict(msgtype="text", text={"content": f"[gf-data-tools] {message}"})
        )
        req = request.Request(url=url, data=data.encode("utf-8"), headers=header)
        ret = request.urlopen(req)
        msg = json.loads(ret.read().decode("utf-8"))
        logging.info(msg)


#  http://sn-list.txwy.tw/dy0zV8P8jkNjG17lb0Pxira5K8IK2YjKpVvKA94WhUE.txt?r=1662796700
# %%
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "region", nargs="+", choices=["ch", "tw", "kr", "us", "jp", "at"]
    )
    parser.add_argument("--force", "-f", action="store_true")
    parser.add_argument("--extract_only", "-e", action="store_true")
    parser.add_argument(
        "--loglevel",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    args = parser.parse_args()
    error = False
    update = False
    for region in args.region:
        print(f"::group::{region.upper()} Server")
        try:
            logging.basicConfig(
                level=args.loglevel,
                stream=sys.stdout,
                format=f"%(asctime)s %(levelname)s: [{region.upper()}] %(message)s",
                force=True,
            )
            data_miner = DataMiner(region)
            u = data_miner.update_raw_resource(args.force, args.extract_only)
            if u:
                update = True
        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Extraction failed due to {e}")
            error = True
        print("::endgroup::")
    if update:
        GithubEnv()["update_detected"] = "true"
    if error:
        raise RuntimeError("Error during execution")
# %%
