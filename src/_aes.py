"""
Minimal, dependency-free AES-128/192/256 CBC decryptor.

Vendored so resolvers can decrypt client-side-encrypted embed payloads
(e.g. vidbasic's /3rdplayer.html CryptoJS scheme) on any platform —
Termux/Android included — without pulling in `cryptography` (heavy Rust
build) or `pycryptodome`. Decrypt-only; that's all the resolvers need.
"""

# AES inverse S-box
_INV_SBOX = None
_SBOX = None
_RCON = (0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36,0x6C,0xD8,0xAB,0x4D)

def _build_sboxes():
    global _SBOX, _INV_SBOX
    if _SBOX is not None:
        return
    p = q = 1
    sbox = [0]*256
    # generate S-box using the standard multiplicative-inverse + affine method
    while True:
        # multiply p by 3
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        # divide q by 3 (multiply by 0xf6)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q ^= 0x09 if q & 0x80 else 0
        q &= 0xFF
        xformed = q ^ ((q << 1)|(q >> 7)) ^ ((q << 2)|(q >> 6)) ^ ((q << 3)|(q >> 5)) ^ ((q << 4)|(q >> 4))
        sbox[p] = (xformed ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0]*256
    for i, v in enumerate(sbox):
        inv[v] = i
    _SBOX = sbox
    _INV_SBOX = inv

def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF

def _mul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a = _xtime(a)
    return r

def _key_expansion(key):
    _build_sboxes()
    nk = len(key) // 4
    nr = {4: 10, 6: 12, 8: 14}[nk]
    w = [list(key[4*i:4*i+4]) for i in range(nk)]
    for i in range(nk, 4*(nr+1)):
        temp = list(w[i-1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]                      # RotWord
            temp = [_SBOX[b] for b in temp]                 # SubWord
            temp[0] ^= _RCON[i//nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([w[i-nk][j] ^ temp[j] for j in range(4)])
    # group into round-key matrices
    return [w[4*r:4*r+4] for r in range(nr+1)], nr

def _add_round_key(state, rk):
    for c in range(4):
        for r in range(4):
            state[r][c] ^= rk[c][r]

def _inv_sub_bytes(state):
    for r in range(4):
        for c in range(4):
            state[r][c] = _INV_SBOX[state[r][c]]

def _inv_shift_rows(state):
    for r in range(1, 4):
        state[r] = state[r][-r:] + state[r][:-r]

def _inv_mix_columns(state):
    for c in range(4):
        a = [state[r][c] for r in range(4)]
        state[0][c] = _mul(a[0],14) ^ _mul(a[1],11) ^ _mul(a[2],13) ^ _mul(a[3],9)
        state[1][c] = _mul(a[0],9)  ^ _mul(a[1],14) ^ _mul(a[2],11) ^ _mul(a[3],13)
        state[2][c] = _mul(a[0],13) ^ _mul(a[1],9)  ^ _mul(a[2],14) ^ _mul(a[3],11)
        state[3][c] = _mul(a[0],11) ^ _mul(a[1],13) ^ _mul(a[2],9)  ^ _mul(a[3],14)

def _decrypt_block(block, round_keys, nr):
    state = [[block[r + 4*c] for c in range(4)] for r in range(4)]
    _add_round_key(state, round_keys[nr])
    for rnd in range(nr-1, 0, -1):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, round_keys[0])
    return bytes(state[r][c] for c in range(4) for r in range(4))

def aes_cbc_decrypt(ciphertext, key, iv, unpad=True):
    """AES-CBC decrypt. key: 16/24/32 bytes, iv: 16 bytes. Returns plaintext
    bytes, PKCS7-unpadded by default."""
    if len(ciphertext) % 16 != 0 or not ciphertext:
        raise ValueError("ciphertext not a multiple of 16 bytes")
    round_keys, nr = _key_expansion(key)
    out = bytearray()
    prev = bytes(iv)
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i+16]
        dec = _decrypt_block(block, round_keys, nr)
        out.extend(b ^ p for b, p in zip(dec, prev))
        prev = block
    if unpad:
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad])*pad:
            out = out[:-pad]
    return bytes(out)
