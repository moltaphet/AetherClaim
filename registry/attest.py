"""Cadastral-authority attestation tooling for AetherClaim.

`AetherClaim.verify_plot_provenance` only marks a plot insurable if it carries an
Ed25519 signature from the CADASTRAL AUTHORITY key that was fixed at deployment.
The signature covers the plot id, the SHA-256 cadastral commitment (which itself
covers coordinates, the 12 baselines, crop cycle, coverage cap and GHCN station),
the eligible policyholder and the authority's own record id for the parcel:

    AETHERCLAIM-ATTESTATION-V1|<registry_record_id>|<plot_id>|<cadastral_hash>|<holder, lower-case 0x hex>

This module is the AUTHORITY side: key handling and signing. It is deliberately
separate from the contract (which only verifies) and from the admin account (which
never holds the authority key). It is a self-contained RFC 8032 Ed25519
implementation so it runs on a bare Python install; `check` cross-verifies with
the `cryptography` package when it is installed.

    python3 registry/attest.py keygen  [--key PATH]      # create a key, print the public key
    python3 registry/attest.py pubkey  [--key PATH]
    python3 registry/attest.py build   [--key PATH]      # rewrite registry/attestations.json
    python3 registry/attest.py check                     # verify attestations.json, exit 1 if bad

The default key path is ~/.aetherclaim/cadastral_authority.seed (outside the repo,
mode 600). Ed25519 signatures are deterministic, so `build` is reproducible.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

ATTESTATION_SCHEMA = "AETHERCLAIM-ATTESTATION-V1"
DEFAULT_KEY = Path.home() / ".aetherclaim" / "cadastral_authority.seed"
HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "cadastral_manifest.json"
ATTESTATIONS = HERE / "attestations.json"

# The authority's own identifier for each parcel (what a registry would call the
# cadastral reference). Opaque to the contract; bound into the signed message.
GENESIS_RECORD_IDS = {
    "CV-ALMOND-01": "DCA-PARCEL-000001",
    "IA-CORN-01": "DCA-PARCEL-000002",
    "AND-OLIVE-01": "DCA-PARCEL-000003",
}

# --------------------------------------------------------------- RFC 8032 Ed25519
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512_mod_q(s: bytes) -> int:
    return int.from_bytes(hashlib.sha512(s).digest(), "little") % _Q


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p):
    r = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            r = _add(r, p)
        p = _add(p, p)
        s >>= 1
    return r


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
assert _GX is not None
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(p) -> bytes:
    zinv = pow(p[2], _P - 2, _P)
    x, y = p[0] * zinv % _P, p[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _equal(p, q) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _expand(seed: bytes):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _sha512_mod_q(prefix + msg)
    rs = _compress(_mul(r, _G))
    h = _sha512_mod_q(rs + pub + msg)
    return rs + ((r + h * a) % _Q).to_bytes(32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    a_pt = _decompress(pub)
    r_pt = _decompress(sig[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _Q:
        return False
    h = _sha512_mod_q(sig[:32] + pub + msg)
    return _equal(_mul(s, _G), _add(r_pt, _mul(h, a_pt)))


# ---------------------------------------------------------------- attestations
def attestation_message(registry_record_id: str, plot_id: str, cadastral_hash: str, holder: str) -> bytes:
    return "|".join(
        [ATTESTATION_SCHEMA, registry_record_id, plot_id, cadastral_hash.lower(), holder.lower()]
    ).encode("utf-8")


def attest(seed: bytes, registry_record_id: str, plot_id: str, cadastral_hash: str, holder: str) -> str:
    return sign(seed, attestation_message(registry_record_id, plot_id, cadastral_hash, holder)).hex()


def load_seed(path: Path) -> bytes:
    seed = bytes.fromhex(path.read_text().strip())
    if len(seed) != 32:
        raise SystemExit(f"{path}: expected a 32-byte hex seed")
    return seed


def build(seed: bytes) -> dict:
    manifest = json.loads(MANIFEST.read_text())
    out = {}
    for pid, rec in manifest["plots"].items():
        rid = GENESIS_RECORD_IDS[pid]
        out[pid] = {
            "registry_record_id": rid,
            "cadastral_hash": rec["cadastral_hash"],
            "eligible_policyholder": rec["eligible_policyholder"],
            "signature": attest(seed, rid, pid, rec["cadastral_hash"], rec["eligible_policyholder"]),
        }
    return {
        "schema": ATTESTATION_SCHEMA,
        "authority_public_key": public_key(seed).hex(),
        "attestations": out,
    }


def check() -> int:
    doc = json.loads(ATTESTATIONS.read_text())
    manifest = json.loads(MANIFEST.read_text())
    pub = bytes.fromhex(doc["authority_public_key"])
    ok = True
    for pid, rec in manifest["plots"].items():
        a = doc["attestations"].get(pid)
        if a is None:
            print(f"{pid}: MISSING")
            ok = False
            continue
        good = (
            a["cadastral_hash"] == rec["cadastral_hash"]
            and a["eligible_policyholder"] == rec["eligible_policyholder"]
            and verify(
                pub,
                attestation_message(a["registry_record_id"], pid, a["cadastral_hash"], a["eligible_policyholder"]),
                bytes.fromhex(a["signature"]),
            )
        )
        try:  # independent cross-check when the reference library is present
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            try:
                Ed25519PublicKey.from_public_bytes(pub).verify(
                    bytes.fromhex(a["signature"]),
                    attestation_message(a["registry_record_id"], pid, a["cadastral_hash"], a["eligible_policyholder"]),
                )
            except InvalidSignature:
                good = False
        except ImportError:
            pass
        print(f"{pid}: {'ok' if good else 'INVALID'}")
        ok = ok and good
    return 0 if ok else 1


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    key = Path(argv[argv.index("--key") + 1]) if "--key" in argv else DEFAULT_KEY
    if cmd == "keygen":
        if key.exists():
            raise SystemExit(f"{key} already exists; refusing to overwrite")
        key.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(os.urandom(32).hex() + "\n")
        print(public_key(load_seed(key)).hex())
        return 0
    if cmd == "pubkey":
        print(public_key(load_seed(key)).hex())
        return 0
    if cmd == "build":
        text = json.dumps(build(load_seed(key)), indent=2) + "\n"
        ATTESTATIONS.write_text(text)
        print(f"wrote {ATTESTATIONS}")
        return 0
    if cmd == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
