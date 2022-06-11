from Crypto.Hash import MD5
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

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