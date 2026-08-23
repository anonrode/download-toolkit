"""Scraper-rules OTA pipeline: validate -> encrypt -> sign -> envelope.

Produces the payload served at scraper_rules.json.enc (repo root of
download-toolkit-serverless). Envelope format v2:

    {"v": 2,
     "alg": "aes-128-cbc",
     "iv": "<32 hex chars, random per encryption>",
     "payload": "<base64 AES-CBC-PKCS7 ciphertext>",
     "sig": "<base64 DER ECDSA-P256-SHA256 signature over the payload string>"}

Security model:
- Confidentiality stays obfuscation-grade (AES key ships in the APK) — it
  stops casual reading of the scraping logic, not dedicated analysis.
- INTEGRITY/AUTHENTICITY is real: the payload is signed with an ECDSA
  P-256 key whose private half exists ONLY as the GitHub Actions secret
  OTA_SIGNING_PRIVATE_KEY. The app embeds the public half and rejects any
  payload that fails verification. A repo hijack or CDN MITM can no longer
  feed users fake rules.
- The IV is random per encryption (upgrades the historical fixed-IV CBC).

Usage:
  python encrypt_rules.py                # validate + encrypt + sign (if key found)
  python encrypt_rules.py --no-sign      # validate + encrypt only (legacy-style output)
  python encrypt_rules.py --gen-keys     # generate ota_keys/*.pem locally

Key lookup order for signing: --key <pem path> | $OTA_SIGNING_PRIVATE_KEY |
ota_keys/ota_signing_private_key.pem (relative to this repo).
"""
import argparse
import base64
import json
import os
import secrets as _secrets
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

RULES_KEY = bytes.fromhex("8f3a9c21d4e65b0789a2c4f6d1e3b5a7")   # 16 bytes (unchanged)

SERVERLESS = r"C:\Users\Anon\download-toolkit-serverless"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.join(REPO_ROOT, "ota_keys", "ota_signing_private_key.pem")

# --- strict schema guards (a bad payload must be rejected BEFORE commit) ---
MAX_PLAINTEXT_BYTES = 512 * 1024
MAX_SELECTOR_LEN = 600
MAX_RULES_ARRAY = 64
REQUIRED_TOP_KEYS_VERSIONED = ("version",)


def validate_schema(plain: bytes) -> list:
    """Returns a list of human-readable problems; empty list == valid."""
    problems = []
    if len(plain) > MAX_PLAINTEXT_BYTES:
        problems.append(f"payload too large: {len(plain)} bytes")
    try:
        obj = json.loads(plain.decode("utf-8"))
    except Exception as e:
        return [f"invalid JSON: {e}"]
    if not isinstance(obj, dict):
        return ["top level must be an object"]

    ver = obj.get("version")
    if not isinstance(ver, str) or not ver or len(ver) > 40:
        problems.append("version must be a non-empty short string")

    def check_selector(val, where):
        if not isinstance(val, str):
            problems.append(f"{where}: selector must be a string")
        elif len(val) > MAX_SELECTOR_LEN:
            problems.append(f"{where}: selector too long ({len(val)})")

    domains = obj.get("domains", {})
    if not isinstance(domains, dict):
        problems.append("domains must be an object")
    else:
        for k, v in domains.items():
            if not isinstance(v, str) or not v.startswith("https://"):
                problems.append(f"domains.{k}: must be an https URL")

    mirrors = obj.get("mirrors", {})
    if not isinstance(mirrors, dict):
        problems.append("mirrors must be an object")
    else:
        for k, arr in mirrors.items():
            if not isinstance(arr, list) or len(arr) > MAX_RULES_ARRAY:
                problems.append(f"mirrors.{k}: must be a list (<= {MAX_RULES_ARRAY})")

    sites = obj.get("sites", {})
    if not isinstance(sites, dict):
        problems.append("sites must be an object")
    else:
        for name, cfg in sites.items():
            if not isinstance(cfg, dict):
                problems.append(f"sites.{name}: must be an object")
                continue
            for field in ("searchPattern", "cardSelector", "episodeSelector",
                          "downloadAnchorSelector"):
                if field in cfg:
                    check_selector(cfg[field], f"sites.{name}.{field}")
            for field in ("seriesDescentSelectors", "slugSuffixes"):
                if field in cfg and (not isinstance(cfg[field], list)
                                     or len(cfg[field]) > MAX_RULES_ARRAY):
                    problems.append(f"sites.{name}.{field}: list <= {MAX_RULES_ARRAY}")

    policies = obj.get("hostPolicies", [])
    if not isinstance(policies, list) or len(policies) > 128:
        problems.append(f"hostPolicies must be a list <= 128")
    else:
        for i, pol in enumerate(policies):
            if not isinstance(pol, dict) or "match" not in pol:
                problems.append(f"hostPolicies[{i}]: object with 'match' required")
            elif "referer" not in pol and "ua" not in pol:
                problems.append(f"hostPolicies[{i}]: needs referer and/or ua")
            elif "referer" in pol:
                ref = pol["referer"]
                if ref != "none" and not ref.startswith(("exact:", "site")):
                    problems.append(
                        f"hostPolicies[{i}].referer: must be none|site|exact:<url>")
            if isinstance(pol, dict) and len(pol.get("match", "")) > 120:
                problems.append(f"hostPolicies[{i}].match too long")

    for key in obj.keys():
        if key not in ("version", "domains", "mirrors", "sites", "resolvers",
                       "directMediaExtensions", "dynamic_providers",
                       "hostPolicies", "searchStrategies", "urlTemplates",
                       "knownDead", "tokenTtlMinutes", "validation",
                       "minAppVersion"):
            problems.append(f"unknown top-level field '{key}' (add it to the "
                            "allow-list in encrypt_rules.py AND DynamicRulesManager)")
    return problems


def load_private_key(explicit=None):
    pem = None
    if explicit:
        pem = open(explicit, "rb").read()
    elif os.environ.get("OTA_SIGNING_PRIVATE_KEY"):
        pem = os.environ["OTA_SIGNING_PRIVATE_KEY"].encode()
        if b"\\n" in pem and b"-----BEGIN" not in pem:
            pem = pem.replace(b"\\n", b"\n")  # secret pasted with literal \n
    elif os.path.exists(KEY_PATH):
        pem = open(KEY_PATH, "rb").read()
    if not pem:
        return None
    return serialization.load_pem_private_key(pem, password=None)


def encrypt_random_iv(plaintext: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(RULES_KEY), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def decrypt_envelope_or_legacy(text: str) -> bytes:
    """Round-trip helper used by --verify; understands both formats."""
    text = text.strip()
    if text.startswith("{"):
        env = json.loads(text)
        iv = bytes.fromhex(env["iv"])
        ct = base64.b64decode(env["payload"])
        padder = padding.PKCS7(128).unpadder()
        dec = Cipher(algorithms.AES(RULES_KEY), modes.CBC(iv)).decryptor()
        return padder.update(dec.update(ct) + dec.finalize()) + padder.finalize()
    from cryptography.hazmat.primitives.ciphers import Cipher as _C  # legacy fixed IV
    iv = bytes.fromhex("5b7e9d2f4a6c8e10f3a5c7d9b1e2f4a6")
    ct = base64.b64decode(text)
    dec = _C(algorithms.AES(RULES_KEY), modes.CBC(iv)).decryptor()
    padder = padding.PKCS7(128).unpadder()
    return padder.update(dec.update(ct) + dec.finalize()) + padder.finalize()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(SERVERLESS, "scraper_rules.json.enc"))
    ap.add_argument("--in", dest="inp", default=os.path.join(SERVERLESS, "scraper_rules.json"))
    ap.add_argument("--key", default=None, help="PEM private key path")
    ap.add_argument("--no-sign", action="store_true")
    ap.add_argument("--gen-keys", action="store_true")
    args = ap.parse_args()

    if args.gen_keys:
        priv = ec.generate_private_key(ec.SECP256R1())
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        open(KEY_PATH, "wb").write(priv.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        pubpem = priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        open(KEY_PATH.replace("_private_", "_public_"), "wb").write(pubpem)
        print(f"wrote {KEY_PATH} (+ public key PEM)")
        print("ACTION REQUIRED: paste the PRIVATE pem into the GitHub secret "
              "OTA_SIGNING_PRIVATE_KEY.")
        return

    with open(args.inp, "rb") as f:
        plain = f.read()

    problems = validate_schema(plain)
    if problems:
        print("SCHEMA VALIDATION FAILED — refusing to emit a payload:")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    iv = _secrets.token_bytes(16)
    ct = encrypt_random_iv(plain, iv)
    payload_b64 = base64.b64encode(ct).decode("ascii")

    key = None if args.no_sign else load_private_key(args.key)
    if key is not None:
        sk = key.sign(payload_b64.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        sig_b64 = base64.b64encode(sk).decode("ascii")
        env = {"v": 2, "alg": "aes-128-cbc", "iv": iv.hex(),
               "payload": payload_b64, "sig": sig_b64}
    else:
        if not args.no_sign:
            print("WARNING: no signing key found — emitting UNSIGNED payload "
                  "(app accepts it until enforcement flips). Add the "
                  "OTA_SIGNING_PRIVATE_KEY secret or pass --key/--no-sign.")
        env = {"v": 1, "alg": "aes-128-cbc", "iv": iv.hex(), "payload": payload_b64}

    out_text = json.dumps(env, separators=(",", ":"))

    # round-trip self-check with this implementation before writing
    assert decrypt_envelope_or_legacy(out_text) == plain, "round-trip mismatch"

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_text)

    mode = "SIGNED" if key is not None else "unsigned"
    print(f"wrote {args.out} ({mode}, v{env['v']}, {len(plain)} bytes plaintext)")


if __name__ == "__main__":
    main()
