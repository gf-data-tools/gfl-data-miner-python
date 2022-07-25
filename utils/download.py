import os
from urllib import request
from urllib.error import URLError
import logging
import socket

socket.setdefaulttimeout(30)

def download(url, path):
    os.makedirs(os.path.split(path)[0],exist_ok=True)
    for _ in range(10):
        try:
            if not os.path.exists(path):
                logging.debug(f'start downloading {url} to {path}')
                request.urlretrieve(url,path+'.tmp')
                os.rename(path+'.tmp',path)
                logging.debug(f'successfully downloaded {path}')
            else:
                logging.warning(f'{path} already exists, skip downloading')
        except Exception as e:
            logging.warning(f'download {path} failed, retrying')
            logging.warning(f'Exception: {e}')
            continue
        else:
            break
    else:
        raise URLError("Reached max retry time, download failed")
    return path

def download_multitask(x):
    return download(*x)