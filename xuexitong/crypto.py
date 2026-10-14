"""AES 加密 —— 复刻学习通登录页 login.js 的 encryptByAES。

官方实现（passport2-static.chaoxing.com/js/fanya/login.js）：

    function encryptByAES(message, key) {
      let CBCOptions = { iv: CryptoJS.enc.Utf8.parse(key),
                         mode: CryptoJS.mode.CBC,
                         padding: CryptoJS.pad.Pkcs7 };
      let encrypted = CryptoJS.AES.encrypt(CryptoJS.enc.Utf8.parse(message),
                                           CryptoJS.enc.Utf8.parse(key), CBCOptions);
      return CryptoJS.enc.Base64.stringify(encrypted.ciphertext);   // 注意：Base64，不是 hex
    }
    const transferKey = "u2oh6Vu^HWe4_AES";   // 16 字节 → AES-128，key 同时当 IV

对照测试见 scripts/check_crypto_vs_js.py（用真 crypto-js 跑同一函数比对）。
"""

from __future__ import annotations

TRANSFER_KEY = b"u2oh6Vu^HWe4_AES"  # 16B：既是 key 也是 IV

__all__ = ["TRANSFER_KEY", "encrypt", "decrypt"]


def _aes():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def _cipher(key: bytes):
        return Cipher(algorithms.AES(key), modes.CBC(key))
    return _cipher


def encrypt(plaintext: str, key: bytes = TRANSFER_KEY) -> str:
    """明文 → Base64(AES-128-CBC/PKCS7, IV=key)，与官方 JS 输出逐字节一致。"""
    import base64

    from cryptography.hazmat.primitives import padding as sym_pad

    if len(key) not in (16, 24, 32):
        raise ValueError(f"AES key 长度非法: {len(key)}")
    padder = sym_pad.PKCS7(128).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = _aes()(key).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


def decrypt(ciphertext_b64: str, key: bytes = TRANSFER_KEY) -> str:
    """仅供测试做往返校验，正式流程不需要。"""
    import base64

    from cryptography.hazmat.primitives import padding as sym_pad

    dec = _aes()(key).decryptor()
    raw = dec.update(base64.b64decode(ciphertext_b64)) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(raw) + unpadder.finalize()).decode("utf-8")
