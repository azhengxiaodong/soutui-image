"""重装后显式恢复发布签名：读取本地口令和加密PKCS8，核对公钥后用新Windows用户DPAPI保存。

不登录GitHub，不上传资料，不输出私钥或口令，不覆盖不同的既有签名密钥。
"""

from pathlib import Path
import argparse
import base64
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-file", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "src"))
    import json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from soutui_image.web_session import _protect
    public = json.loads((root / "src/soutui_image/resources/update_source.json").read_text(encoding="utf-8"))["public_key"]
    key = serialization.load_pem_private_key((root / "migration_backup/private/update_signing_key.encrypted.pem").read_bytes(),
                                            args.password_file.read_text(encoding="utf-8").strip().encode())
    actual = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    assert actual == public, "公钥不匹配，不能恢复"
    destination = root / "data/local/update_signing_key.dpapi"
    if destination.exists():
        old = Ed25519PrivateKey.from_private_bytes(_protect(destination.read_bytes(), decrypt=True))
        assert old.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) == key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print("当前用户已有同一发布签名，无需覆盖。")
        return
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(_protect(raw))
    assert _protect(destination.read_bytes(), decrypt=True) == raw
    print("发布签名已恢复到当前Windows用户；未联网或发布更新。")


if __name__ == "__main__":
    main()
