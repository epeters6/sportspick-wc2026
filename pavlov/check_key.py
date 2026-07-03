import sys
print('Python:', sys.version)
import cryptography
print('cryptography:', cryptography.__version__)

with open('data/kalshi_private_key.pem', 'rb') as f:
    raw = f.read()

print(f'File size: {len(raw)} bytes')
print(f'First 4 bytes hex: {raw[:4].hex()}')
print(f'Has CRLF: {b"\\r\\n" in raw}')
print(f'Has lone CR: {b"\\r" in raw.replace(b"\\r\\n", b"")}')
print(f'Has BOM: {raw.startswith(bytes([0xef,0xbb,0xbf]))}')
print(f'First line raw: {raw.split(b"\\n")[0]}')
print(f'Last non-empty line: {[l for l in raw.split(b"\\n") if l.strip()][-1]}')
print(f'Total lines: {len(raw.split(b"\\n"))}')

# Try loading without backend param
from cryptography.hazmat.primitives import serialization
clean = raw
if clean.startswith(bytes([0xef, 0xbb, 0xbf])):
    clean = clean[3:]
clean = clean.replace(b'\r\n', b'\n').replace(b'\r', b'\n')

try:
    key = serialization.load_pem_private_key(clean, password=None)
    print('SUCCESS: Key loaded as', type(key).__name__)
    print('Key size:', key.key_size, 'bits')
except Exception as e:
    print('FAILED:', e)
    # Show first 300 chars of cleaned PEM
    print('Cleaned PEM start:', clean[:200])
