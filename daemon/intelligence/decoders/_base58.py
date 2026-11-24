"""Base58 (Bitcoin alphabet) encode/decode — pure stdlib.

No `base58` package exists in the venv and the AGT prefix cannot be
assumed to have the venv's packages, so the decoders carry their own
implementation rather than adding a dependency. Used for pubkeys,
signatures and raw instruction data.
"""
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(text):
    """Decode a base58 string to bytes. Raises ValueError on bad input.

    Leading '1's decode to leading zero bytes. Empty string decodes
    to b"".
    """
    if not isinstance(text, str):
        raise ValueError("base58 input must be str, got %r"
                         % type(text).__name__)
    if text == "":
        return b""
    n = 0
    for ch in text:
        try:
            digit = _B58_INDEX[ch]
        except KeyError:
            raise ValueError("invalid base58 character %r" % ch)
        n = n * 58 + digit
    # Leading '1's are leading zero bytes; the integer conversion
    # drops them, so count them back in.
    pad = 0
    for ch in text:
        if ch == "1":
            pad += 1
        else:
            break
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + raw


def b58encode(data):
    """Encode bytes to a base58 string. Raises ValueError on bad input."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("base58 input must be bytes, got %r"
                         % type(data).__name__)
    raw = bytes(data)
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    n = int.from_bytes(raw, "big") if raw else 0
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(_B58_ALPHABET[rem])
    return "1" * pad + "".join(reversed(out))


def is_b58(text):
    """True when `text` is a non-empty string of base58 characters."""
    return isinstance(text, str) and bool(text) and all(
        c in _B58_INDEX for c in text)
