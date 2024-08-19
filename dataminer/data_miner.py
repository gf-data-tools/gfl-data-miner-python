import base64
import io
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import cached_property
from gzip import GzipFile
from pathlib import Path
from typing import *
from urllib import request
from zipfile import ZipFile

import git
import hjson
import urllib3
from gf_utils2.gamedata import GameData
from gf_utils.crypto import get_des_encrypted, get_md5_hash, xor_decrypt
from gf_utils.download import download
from git.repo import Repo
from logger_tt import logger

from utils.asset_extractor import unpack_all_assets
from utils.format_stc import format_stc


class GithubEnv:
    def __init__(self):
        self.file = Path(os.environ.get("GITHUB_ENV", "env.txt"))

    def __setitem__(self, name: str, value: Any):
        with self.file.open("a") as f:
            f.write(f"{name}={value}\n")


@dataclass
class DataMiner:
    region: Literal["tw", "at", "ch", "kr", "jp", "us"] = "ch"
    data_dir: Path = os.devnull
    github_repo: str = ""
    github_token: str = ""
    git_author: str = ""
    res_key: str = "kxwL8X2+fgM="
    res_iv: str = "M9lp+7j2Jdwqr+Yj1h+A"
    lua_key: str = "lvbb3zfc3faa8mq1rx0r0gl61b4338fa"
    dat_key: str = "c88d016d261eb80ce4d6e41a510d4048"
    dingtalk_token: str = ""
    qq_channel: str = ""
    qq_token: str = ""

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.tmp_dir = tempfile.TemporaryDirectory()

    def clone_repo(self):
        assert self.github_repo, "github_repo not set"
        if not self.github_token:
            url = f"https://github.com/{self.github_repo}.git"
        else:
            url = (
                f"https://oauth2:{self.github_token}@github.com/{self.github_repo}.git"
            )
        logger.info(f"Clonning {self.github_repo}")
        repo = Repo.clone_from(url, to_path=self.data_dir, depth=1)
        repo.remote().set_url(url)
        return repo

    def read_repo_file(self, filepath):
        if self.data_dir.exists():
            return (self.data_dir / filepath).read_text()
        else:
            logger.info(f"Getting {filepath} from remote")
            req = request.Request(
                f"https://raw.githubusercontent.com/{self.github_repo}/main/{filepath}",
                headers={
                    "Authorization": (
                        f"Bearer {self.github_token}" if self.github_token else ""
                    )
                },
            )
            return request.urlopen(req).read().decode("utf-8")

    @cached_property
    def repo(self) -> Repo:
        if self.data_dir.exists():
            repo = Repo(self.data_dir)
        else:
            repo = self.clone_repo()
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "github-actions[bot]")
            cw.set_value("user", "email", "<>")
        return repo

    @property
    def version_str(self):
        s = self.resdata["daBaoTime"][:-32]
        if s.endswith("_"):
            s = s[:-1]
        d, h, m = s.split("_")
        db_time = f"{d}_{h:>02}_{m:>02}"
        return (
            f"{self.region.upper()} "
            f"| {self.client_version} "
            f'| data {self.index_version["data_version"][:7]} '
            f"| dabao {db_time}"
        )

    def commit_repo(self, push=False):
        version_info = dict(
            data_version=self.index_version["data_version"],
            client_version=self.client_version,
            ab_version=self.index_version["ab_version"],
            dabao_time=self.resdata["daBaoTime"],
        )
        (self.data_dir / "version.json").write_text(json.dumps(version_info, indent=2))
        (self.data_dir / "resdata_no_hash.json").write_text(
            json.dumps(self.resdata, indent=2, ensure_ascii=False)
        )

        message = self.version_str

        try:
            self.repo.git.add(all=True)
            commit_msg = self.repo.git.commit(
                m=message, author=self.git_author or "github-actions[bot] <>"
            )
            logger.info(commit_msg)
            if push:
                self.repo.remote().push()
                self.dingtalk_notice(message)
                self.qq_notice(message)
                return True
        except git.GitCommandError as e:
            logger.error(e)
        return False

    def dingtalk_notice(self, message: str):
        if not self.dingtalk_token:
            logger.warning(f'Cannot send message "{message}"')
            return
        url = f"https://oapi.dingtalk.com/robot/send?access_token={self.dingtalk_token}"
        header = {"Content-Type": "application/json"}
        data = json.dumps(
            dict(msgtype="text", text={"content": f"[gf-data-tools] {message}"})
        )
        req = request.Request(url=url, data=data.encode("utf-8"), headers=header)
        ret = request.urlopen(req)
        msg = json.loads(ret.read().decode("utf-8"))
        logger.info(f'Send dingtalk message "{message}"')
        logger.info(f"Return: {msg}")

    def qq_notice(self, message: str):
        if not (self.qq_channel and self.qq_token):
            logger.warning(f'Cannot send message "{message}"')
            return
        try:
            req = request.Request(
                url=f"https://sandbox.api.sgroup.qq.com/channels/{self.qq_channel}/messages",
                headers={
                    "Authorization": f"Bot {self.qq_token}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(dict(content=message)).encode(),
            )

            resp = request.urlopen(req)
            data = json.loads(resp.read().decode())
            logger.info(data)
        except Exception as e:
            logger.exception(repr(e))

    @cached_property
    def hosts(self):
        return hjson.loads(self.read_repo_file("hosts.json5"))

    @cached_property
    def index_version(self):
        logger.info(f"Requesting version")
        version_url = self.host_server + "Index/version"
        logger.info(version_url)
        response = request.urlopen(version_url).read().decode()
        logger.info(f"Response: {response}")
        return hjson.loads(response)

    @cached_property
    def client_version_(self):
        return self.index_version["client_version"]

    @cached_property
    def server_info(self):
        https = urllib3.PoolManager(cert_reqs="CERT_NONE")

        resp = https.request(
            method="POST",
            url=self.hosts["transit_host"],
            fields={
                "c": "game",
                "a": "newserverList",
                "channel": self.hosts["channel"],
                "check_version": "0",
            },
            headers={},
        )
        data = resp.data.decode()
        logger.info(self.hosts["transit_host"] + "\n" + data)
        tree = ET.parse(io.StringIO(data))
        return tree

    @cached_property
    def host_server(self):
        server = self.server_info.getroot().find("./server/addr").text
        logger.info(f"Server Addr: {server}")
        return server

    @cached_property
    def client_version(self):
        client = self.server_info.getroot().find("./config/client_version").text
        logger.info(f"Client Version: {client}")
        return client

    @cached_property
    def min_version(self):
        client_version = int(self.client_version)
        min_version = round(client_version / 10)
        logger.info(f"Min Version: {min_version}")
        return min_version

    @property
    def ab_version(self):
        return self.index_version["ab_version"]

    @property
    def local_version(self):
        return hjson.loads(self.read_repo_file("version.json"))

    def clear_local_data(self):
        for content in self.data_dir.iterdir():
            if content.is_dir() and content.name != ".git":
                shutil.rmtree(content)

    def download_stc(self, data_version=None):
        logger.info(f"Downloading stc data")
        if data_version is None:
            data_version = self.index_version["data_version"]
        hash = get_md5_hash(data_version)
        stc_url = f"{self.hosts['cdn_host']}/data/stc_{data_version}{hash}.zip"
        logger.info(stc_url)
        stc_fp = os.path.join(self.tmp_dir.name, "stc.zip")
        download(stc_url, stc_fp)
        ZipFile(stc_fp).extractall(os.path.join(self.tmp_dir.name, "stc"))

    def process_catchdata(self):
        logger.info(f"Decoding catchdata")
        dst_dir = self.data_dir / "catchdata"
        dst_dir.mkdir(parents=True, exist_ok=True)

        with open(os.path.join(self.tmp_dir.name, "stc/catchdata.dat"), "rb") as f:
            cipher = f.read()
        compressed = xor_decrypt(cipher, self.dat_key)
        plain = GzipFile(fileobj=io.BytesIO(compressed)).read().decode("utf-8")
        logger.info(f"Extracting json from catchdata")
        for json_string in plain.split("\n")[:-1]:
            data = json.loads(json_string)
            assert len(data.keys()) == 1
            for key in data.keys():
                logger.debug(f"Formatting {key}.json")
                with (dst_dir / f"{key}.json").open("w", encoding="utf-8") as f:
                    json.dump(data[key], f, indent=4, ensure_ascii=False)

    def process_stc(self):
        mapping_dir = Path(__file__).parent / f"stc-mapping/{int(self.min_version)}"

        logger.info(f"Reading stc-mapping from {mapping_dir}")
        stc_dir = Path(self.tmp_dir.name) / "stc"
        dst_dir = self.data_dir / "stc"
        dst_dir.mkdir(parents=True, exist_ok=True)

        for f in os.listdir(stc_dir):
            try:
                id, ext = os.path.splitext(f)
                if ext != ".stc":
                    continue
                logger.info(f"Formating {f}")
                stc = stc_dir / f"{id}.stc"
                mapping = mapping_dir / f"{id}.json"
                name, data = format_stc(stc, mapping, self.min_version >= 3020)
                # (Path(dst_dir) / f"{name}.json").write_text(self.json_formatter.serialize(data))
                with (dst_dir / f"{name}.json").open("w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to format {f}")

    @cached_property
    def resdata(self):
        logger.info(f"Getting resource data list")
        bkey = base64.standard_b64decode(self.res_key)
        biv = base64.standard_b64decode(self.res_iv)
        if self.region == "at":
            fname = f"{self.min_version}_alpha2020_{self.ab_version}_AndroidResConfigData2018"
        elif self.region in ["us", "ch", "tw", "kr", "jp"]:
            fname = f"{self.min_version}_{self.ab_version}_AndroidResConfigData2018"
        else:
            fname = f"{self.min_version}_{self.ab_version}_AndroidResConfigData"
        logger.debug(f"resdata name {fname}")

        en = get_des_encrypted(fname, bkey, biv[:8])
        res_config = base64.standard_b64encode(en).decode("utf-8")
        logger.debug(f"encoded {res_config}")
        res_config = re.sub(r"[^a-zA-Z0-9]", "", res_config) + ".txt"
        resdata_url = self.hosts["asset_host"] + "/" + res_config

        tmp_dir = Path(self.tmp_dir.name)

        resdata_fp = tmp_dir / "AndroidResConfigData"
        download(resdata_url, resdata_fp)
        unpack_all_assets(resdata_fp, tmp_dir)
        with open(tmp_dir / "assets/resources/resdata.asset", encoding="utf-8") as f:
            resdata = hjson.load(f)

        for k in ["passivityAssetBundles", "BaseAssetBundles", "AddAssetBundles"]:
            resdata[k].sort(key=lambda x: x["assetBundleName"])
            for r in resdata[k]:
                r["assetAllRes"].sort(key=lambda x: x["pathKey"])
                for a in r["assetAllRes"]:
                    a.pop("hashCode", None)
                    a.pop("hasCodes", None)
        return resdata

    def download_asset_bundles(self):
        res_url = self.resdata["resUrl"]
        targets = [
            "asset_textavg",
            "asset_texttable",
            "asset_textes",
            "asset_textlangue",
            "asset_textlpatch",
            "asset_csv",
        ]
        for ab_info in self.resdata["BaseAssetBundles"]:
            if ab_info["assetBundleName"] in targets:
                ab_url = f'{res_url}{ab_info["resname"]}.ab'
                ab_fp = os.path.join(
                    self.tmp_dir.name, f'{ab_info["assetBundleName"]}.ab'
                )
                download(ab_url, ab_fp)

    def unpack_assets(self):
        logger.info("Processing assets")
        tmp_dir = Path(self.tmp_dir.name)
        for asset_bundle in tmp_dir.glob("*.ab"):
            unpack_all_assets(asset_bundle, tmp_dir)
        for f in tmp_dir.glob("**/*.asset"):
            os.remove(f)
        for file in (tmp_dir / "assets/resources/dabao/luapatch").glob("**/*.txt"):
            logger.debug(f"decrypting {file}")
            cipher = file.read_bytes()
            plain = xor_decrypt(cipher, self.lua_key)
            (file.parent / file.name[:-4]).write_bytes(plain)
            os.remove(file)

        shutil.copytree(tmp_dir / "assets/resources/dabao", self.data_dir / "asset")
        shutil.copytree(
            tmp_dir / "assets/resources/textdata", self.data_dir / "asset/textdata"
        )

    def format_hjson(self):
        logger.info("Formatting hjson for human friendly output")
        format_dir = self.data_dir / "formatted"
        format_dir.mkdir(parents=True, exist_ok=True)
        data = GameData(
            stc_dir=[self.data_dir / tgt for tgt in ["catchdata", "stc"]],
            table_dir=self.data_dir / "asset/table",
            to_dict=False,
        )
        for name, table in data.items():
            for record in table:
                for k, v in dict(record).items():
                    if v == "" or v == "0" or v == 0:
                        record.pop(k)
            (format_dir / f"{name}.hjson").write_text(
                hjson.dumps(table), encoding="utf-8"
            )

    def update_available(self):
        logger.info(self.version_str)
        return (
            self.local_version["data_version"] != self.index_version["data_version"]
            or self.local_version["dabao_time"] != self.resdata["daBaoTime"]
        )
