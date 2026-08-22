"""Encrypt scraper_rules.json -> scraper_rules.json.enc (AES-128-CBC, PKCS7).

The OTA rules payload is encrypted so the scraping logic isn't readable from
the GitHub repo (site owners watching the repo see ciphertext). The key/IV
are embedded in the app (DynamicRulesManager) and mirrored here; this is
obfuscation-grade protection — a determined reverse engineer can extract the
key from the APK — it stops casual reading, not dedicated analysis.

Key/IV must match:
  app/src/main/java/com/anonrode/downloader/data/rules/DynamicRulesManager.kt

Usage: python encrypt_rules.py [--out <path>]
Output: base64(AES-CBC(plaintext)) written to scraper_rules.json.enc next to
the plaintext file.
"""
import argparse
import base64
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

RULES_KEY = bytes.fromhex("8f3a9c21d4e65b0789a2c4f6d1e3b5a7")   # 16 bytes
RULES_IV = bytes.fromhex("5b7e9d2f4a6c8e10f3a5c7d9b1e2f4a6")    # 16 bytes

SERVERLESS = r"C:\Users\Anon\download-toolkit-serverless"


def encrypt(plaintext: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(RULES_KEY), modes.CBC(RULES_IV))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def decrypt(ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(RULES_KEY), modes.CBC(RULES_IV))
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(SERVERLESS, "scraper_rules.json.enc"))
    args = ap.parse_args()

    plain_path = os.path.join(SERVERLESS, "scraper_rules.json")
    with open(plain_path, "rb") as f:
        plain = f.read()

    enc = encrypt(plain)
    # round-trip self-check with the SAME implementation before writing
    assert decrypt(enc) == plain, "round-trip mismatch"

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(enc).decode("ascii"))

    print(f"wrote {args.out} ({len(enc)} bytes ciphertext, "
          f"{len(plain)} bytes plaintext)")


if __name__ == "__main__":
    main()
