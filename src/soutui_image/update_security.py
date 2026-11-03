"""U_GITHUB签名：维护机私钥由DPAPI保护，客户端只持公钥，下载内容校验后才使用。"""

import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .rules_bundle import canonical
from .web_session import _protect


def create_signing_key(path):
    """只在明确发布入口生成一次；私钥不进入发布包或日志。"""
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        key=Ed25519PrivateKey.from_private_bytes(_protect(path.read_bytes(),decrypt=True))
    else:
        key=Ed25519PrivateKey.generate()
        raw=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        with path.open("xb") as handle: handle.write(_protect(raw))
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()


def sign_payload(payload, private_path):
    """输入审核通过的发布清单，输出带Ed25519签名的JSON数据。"""
    raw=_protect(Path(private_path).read_bytes(),decrypt=True)
    key=Ed25519PrivateKey.from_private_bytes(raw)
    return {"payload":payload,"signature":base64.b64encode(key.sign(canonical(payload))).decode()}


def verify_payload(envelope, public_key):
    """公钥由程序固定信任，GitHub下载内容不能自行替换公钥。"""
    if not isinstance(envelope,dict) or set(envelope)!={"payload","signature"}:
        raise ValueError("更新签名格式不符")
    try:
        key=Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key,validate=True))
        key.verify(base64.b64decode(envelope["signature"],validate=True),canonical(envelope["payload"]))
    except (InvalidSignature,ValueError,TypeError) as error:
        raise ValueError("更新签名校验失败，保留原版本") from None
    return envelope["payload"]


def save_read_token(token, path):
    """客户端首次在本机输入只读授权；仅DPAPI文件落盘，不输出明文。"""
    if not isinstance(token,str) or not 20<=len(token)<=8192 or any(ord(c)<33 or ord(c)>126 for c in token):
        raise ValueError("GitHub授权格式无效")
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(".tmp"); temporary.write_bytes(_protect(token.encode("utf-8"))); temporary.replace(path)


def load_read_token(path):
    """只在私有GitHub请求前解密，不把令牌写入运行证据。"""
    return _protect(Path(path).read_bytes(),decrypt=True).decode("utf-8")
