from Crypto.Hash import MD5
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
from Crypto.Cipher import ARC4
from gzip import GzipFile
import base64, io

def get_md5_hash(input:str):
    data = MD5.MD5Hash(input.encode('utf-8')).digest()
    return ('{:02x}'*len(data)).format(*data)

def get_des_encrypted(data, key, iv):
    des = DES.new(key=key,iv=iv,mode=DES.MODE_CBC)
    return des.encrypt(pad(data.encode('utf-8'),block_size=des.block_size))

def xor_decrypt(cipher,key):
    key = key.encode('utf-8')
    lk = len(key)
    plain = [c^key[i%lk] for i,c in enumerate(cipher)]
    return bytes(plain)

def decrypt_rc4(data, key):
    rc4 = ARC4.new(key)
    return rc4.decrypt(data)
    
def decrypt(src,key):
    hash=get_md5_hash(key)
    righthash=get_md5_hash(hash[16:])
    rc4Key = righthash + get_md5_hash(righthash)
    while len(src)%4!=0:
        src+='='
    cipher = base64.standard_b64decode(src)
    plain = decrypt_rc4(cipher,rc4Key.encode('utf-8'))
    return plain

def decode(src,key='yundoudou'):
    if src[0]=='#':
        compressed = decrypt(src[1:],key)
        plain = GzipFile(fileobj=io.BytesIO(compressed[26:])).read().decode('utf-8')
    else:
        plain = decrypt(src,key)
    return plain
