# %%
import pyjson5
import json
import logging
import os
import io
import shutil
from urllib import request
from utils.download import download
from utils.crypto import get_des_encrypted, get_md5_hash, xor_decrypt
from utils.asset_extractor import unpack_all_assets
from utils.format_stc import format_stc
import base64
import re
from gzip import GzipFile
from zipfile import ZipFile
from git import Git
from git.repo import Repo
import argparse
from pathlib import Path
from gf_utils.stc_data import get_stc_data
import pandas as pd
import traceback

os.chdir(Path(__file__).resolve().parent)

CONFIG_JSON5 = r'conf/config.json5'
with open(CONFIG_JSON5,'r',encoding='utf-8') as f:
    conf = pyjson5.load(f)
RAW_ROOT = r'raw'
DATA_ROOT = conf['git']['local']

# %%
class DataMiner():
    def __init__(self,region='ch'):
        self.region=region
        self.host = conf['hosts'][region]
        self.res_key = conf['res_key']
        self.res_iv = conf['res_iv']
        self.lua_key = conf['lua_key']
        self.dat_key = conf['dat_key']
        self.raw_dir = os.path.join(RAW_ROOT,region)
        self.data_dir = os.path.join(DATA_ROOT,region)

    def get_current_version(self):
        version_url = self.host['game_host'] + '/Index/version'
        logging.info(f'requesting version from {version_url}')
        response = request.urlopen(version_url)
        version = pyjson5.loads(response.read().decode())
        self.version = version
        self.dataVersion = version["data_version"]
        self.clientVersion = version["client_version"]
        self.minversion = round(eval(self.clientVersion)/100) * 10
        self.abVersion = version["ab_version"]

    def get_res_data(self):
        bkey = base64.standard_b64decode(self.res_key)
        biv = base64.standard_b64decode(self.res_iv)
        fname = f"{self.minversion}_{self.abVersion}_AndroidResConfigData"

        en = get_des_encrypted(fname,bkey,biv[:8])
        res_config = base64.standard_b64encode(en).decode('utf-8')
        res_config = re.sub(r"[^a-zA-Z0-9]","",res_config)+'.txt'
        resdata_url = self.host['asset_host']+'/'+res_config
        resdata_fp = os.path.join(self.raw_dir,'AndroidResConfigData')
        download(resdata_url,resdata_fp)
        unpack_all_assets(resdata_fp,self.raw_dir)
        with open(os.path.join(self.raw_dir,'assets/resources/resdata.asset'),encoding='utf-8') as f:
            self.resdata = pyjson5.load(f)
        self.daBaoTime=self.resdata['daBaoTime']
    
    def process_resdata(self):
        shutil.copy(os.path.join(self.raw_dir,'assets/resources/resdata.asset'),os.path.join(self.data_dir,'resdata.json'))
        for k in ['passivityAssetBundles', 'BaseAssetBundles', 'AddAssetBundles']:
            for r in self.resdata[k]:
                for a in r["assetAllRes"]:
                    a.pop("hashCode",None)
                    a.pop("hasCodes",None)
        with open(os.path.join(self.data_dir,'resdata_no_hash.json'),'w',encoding='utf-8') as f:
            json.dump(self.resdata,f,indent=4,ensure_ascii=False)

    def get_asset_bundles(self):
        res_url = self.resdata['resUrl']
        targets = ['asset_textavg','asset_texttable','asset_textes']
        for ab_info in self.resdata['BaseAssetBundles']:
            if ab_info['assetBundleName'] in targets:
                ab_url = f'{res_url}{ab_info["resname"]}.ab'
                ab_fp = os.path.join(self.raw_dir,f'{ab_info["assetBundleName"]}.ab')
                download(ab_url,ab_fp)

    def get_stc(self):
        hash = get_md5_hash(self.dataVersion)
        stc_url = self.host['cdn_host'] + "/data/stc_" + self.dataVersion + hash + ".zip"
        stc_fp = os.path.join(self.raw_dir,'stc.zip')
        download(stc_url,stc_fp)
        ZipFile(stc_fp).extractall(os.path.join(self.raw_dir,'stc'))
    
    def update_raw_resource(self, force=False):
        if os.path.exists(self.raw_dir):
            shutil.rmtree(self.raw_dir)
        os.makedirs(self.raw_dir)
        self.get_current_version()
        self.get_res_data()
        logging.info(f"[{self.region}] client {self.clientVersion} | ab {self.abVersion} | dabao {self.daBaoTime} | data {self.dataVersion}")
            
        available = False
        if not force:
            saved_version_fp = os.path.join(self.data_dir,'version.json')
            if not os.path.exists(saved_version_fp):
                available = True
            else:
                with open(saved_version_fp,encoding='utf-8') as f:
                    version = pyjson5.load(f)
                if version["data_version"] != self.dataVersion:
                    available =True
            saved_resdata_fp = os.path.join(self.data_dir,'resdata.json')
            if not os.path.exists(saved_resdata_fp):
                available = True
            else:
                with open(saved_resdata_fp,encoding='utf-8') as f:
                    resdata = pyjson5.load(f)
                if resdata["daBaoTime"] != self.daBaoTime:
                    available =True
        else:
            available = True

        if available:
            if os.path.exists(self.data_dir):
                shutil.rmtree(self.data_dir)
            os.makedirs(self.data_dir)
            logging.info('new data available, start downloading')
            self.get_asset_bundles()
            self.get_stc()
            self.process_resdata()
            self.process_assets()
            self.process_catchdata()
            self.process_stc()
            self.format_json()
            with open(os.path.join(self.data_dir,'version.json'),'w',encoding='utf-8') as f:
                json.dump(self.version,f,indent=4,ensure_ascii=False)
            git = Git(DATA_ROOT)
            logging.info('committing')
            git.execute(f'git add {self.region}', shell=True)
            response = git.execute(f'git commit -m "[{self.region}] client {self.clientVersion} | ab {self.abVersion} | dabao {self.daBaoTime} | data {self.dataVersion}"', shell=True)
            logging.info(response)
            shutil.rmtree(self.raw_dir)
        else:
            logging.info('current data is up to date')
        return available
        
    def process_assets(self):
        for asset in ['asset_textavg','asset_texttable','asset_textes']:
            unpack_all_assets(os.path.join(self.raw_dir,asset+'.ab'),self.raw_dir)
        asset_output = os.path.join(self.data_dir,'asset')
        os.makedirs(asset_output,exist_ok=True)
        asset_dir = os.path.join(self.raw_dir,'assets/resources/dabao')
        for subdir in os.listdir(asset_dir):
            shutil.copytree(os.path.join(asset_dir,subdir),os.path.join(asset_output,subdir),dirs_exist_ok=True)
        self.decode_luapatch()

    def decode_luapatch(self):
        src_dir = os.path.join(self.data_dir,'asset/luapatch')
        for root,dirs,files in os.walk(src_dir):
            for file in files:
                if not file.endswith('txt'):
                    continue
                logging.info(f'decoding {file}')
                with open(os.path.join(root,file),'rb') as f:
                    cipher = f.read()
                plain = xor_decrypt(cipher,self.lua_key)
                with open(os.path.join(root,file[:-4]),'wb') as f:
                    f.write(plain)
                os.remove(os.path.join(root,file))

     
    def process_catchdata(self):
        logging.info(f'decoding catchdata')
        dst_dir = os.path.join(self.data_dir,'catchdata')
        os.makedirs(dst_dir,exist_ok=True)
        with open(os.path.join(self.raw_dir,'stc/catchdata.dat'),'rb') as f:
            cipher = f.read()
        compressed = xor_decrypt(cipher,self.dat_key)
        plain = GzipFile(fileobj=io.BytesIO(compressed)).read().decode('utf-8')
        with open(os.path.join(dst_dir,'catchdata'),'w',encoding='utf-8') as f:
            f.write(plain)
        for json_string in plain.split('\n')[:-1]:
            data = json.loads(json_string)
            assert len(data.keys()) == 1
            for key in data.keys():
                logging.info(f'Formatting {key}.json from catchdata')
                with open(os.path.join(dst_dir,f'{key}.json'),'w',encoding='utf-8') as f:
                    json.dump(data[key],f,indent=4,ensure_ascii=False)
        
    def process_stc(self):
        mapping_dir = os.path.join('conf/stc-mapping',str(self.minversion))
        logging.info(f'Read stc-mapping from {mapping_dir}')
        stc_dir = os.path.join(self.raw_dir,'stc')
        dst_dir = os.path.join(self.data_dir,'stc')
        os.makedirs(dst_dir,exist_ok=True)

        for f in os.listdir(mapping_dir):
            id, ext = os.path.splitext(f)
            assert ext=='.json'
            stc = os.path.join(stc_dir,f'{id}.stc')
            mapping = os.path.join(mapping_dir,f'{id}.json')
            name, data = format_stc(stc,mapping)
            with open(os.path.join(dst_dir, f'{name}.json'),'w',encoding='utf-8') as f:
                json.dump(data,f,indent=4,ensure_ascii=False)

    def format_csv(self):
        output_dir = os.path.join(self.data_dir,'csv') 
        os.makedirs(output_dir,exist_ok=True)
        table_dir = os.path.join(self.data_dir,'asset/table')
        for j in ['catchdata','stc']:
            json_dir = os.path.join(self.data_dir,j)
            data = get_stc_data(json_dir, table_dir,to_dict=False)
            for key, value in data.items():
                pd.DataFrame.from_records(value).to_csv(os.path.join(output_dir,f'{key}.csv'),index=False)

    def format_json(self):
        output_dir = os.path.join(self.data_dir,'json_with_text') 
        os.makedirs(output_dir,exist_ok=True)
        table_dir = os.path.join(self.data_dir,'asset/table')
        for j in ['catchdata','stc']:
            json_dir = os.path.join(self.data_dir,j)
            data = get_stc_data(json_dir, table_dir,to_dict=False)
            for key, value in data.items():
                with open(os.path.join(output_dir,f'{key}.json'),'w',encoding='utf-8') as f:
                    json.dump(value,f,ensure_ascii=False,indent=4)


# %%
if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('region',nargs='+',choices=['ch','tw','kr','jp','us'])
    parser.add_argument('--force', '-f',action='store_true')
    args=parser.parse_args()
    for region in args.region:
        try:
            logging.basicConfig(level='INFO',format=f'%(asctime)s %(levelname)s: [{region.upper()}] %(message)s',force=True)
            data_miner = DataMiner(region)
            data_miner.update_raw_resource(args.force)
        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Extraction failed due to {e}")
# %%
