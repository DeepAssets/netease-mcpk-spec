"""Filename-key arithmetic for MCPK containers. Author: nmbb.

The byte order, integer constants and state transitions are format behaviour.
This implementation processes the data words and finalizers in one pipeline.
"""
from itertools import chain
from struct import iter_unpack


def hash_bytes(payload: bytes) -> int:
    """Return the 32-bit lookup key of the supplied byte sequence."""
    radix = 2 ** 32
    left, right, rotor = 0x37A8470E, 0x7758B42B, 0xF4FA8928
    padded = payload + bytes((-len(payload)) % 4)
    words = (item[0] for item in iter_unpack('<I', padded))
    for value in chain(words, (0x9BE74448, 0x66F42C48)):
        left ^= value
        right ^= value
        rotor = (rotor * 2) % radix + rotor // (radix // 2)
        salt = rotor ^ 0x267B0B11
        factors = (
            (((right + salt) % radix) & 0xBDEB77DE) | 0x02040801,
            (((left + salt) % radix) & 0x7D7EBBDE) | 0x00804021,
        )
        high_a, low_a = divmod(left * factors[0], radix)
        high_b, low_b = divmod(right * factors[1], radix)
        carry_a, remainder_a = divmod(low_a + high_a + int(high_a != 0), radix)
        carry_b, remainder_b = divmod(low_b + 2 * high_b, radix)
        left = (remainder_a + carry_a) % radix
        right = (remainder_b + 2 * carry_b) % radix
    return left ^ right


def path_key(path: str) -> int:
    """Hash only the last slash-delimited component, encoded as UTF-8."""
    filename = path.rpartition('/')[2]
    return hash_bytes(filename.encode('utf-8'))
