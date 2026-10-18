# phonemirror link protocol, version 1

Used between the phonemirror Android app (the phone) and the computer over Wi-Fi.
The computer listens on TCP port 27199 and advertises `_phonemirror._tcp` over mDNS;
the phone connects when the user taps **Start mirroring**.

## Pairing

The computer creates a pairing and shows it as a QR code (or hands it to the app over
adb with `phonemirror setup android`):

```
phonemirror://pair?v=1&id=<pair id, 32 hex>&k=<key, 32 bytes base64url>&pc=<computer id, 32 hex>
                  &n=<computer name>&h=<ip>[,<ip>...]&p=<port>
```

The app asks the user to confirm before it saves the pairing. The key `K` never travels over
the network after that. Both sides keep it in private storage (Android: encrypted with an
Android Keystore key; computer: `pairings.json`, mode 0600).

## Handshake

All integers are big-endian. `H` is SHA-256, `HMAC` is HMAC-SHA256, public keys are P-256
SubjectPublicKeyInfo DER.

```
phone -> computer   ClientHello = "PMIR" | 0x01 | pair_id (16) | nonce_c (32) | len (u16) | epk_c
computer -> phone   ServerHello = "PMIR" | 0x01 | nonce_s (32) | len (u16) | epk_s | mac_s (32)
phone -> computer   mac_c (32)

T      = ClientHello | ServerHello without mac_s
mac_s  = HMAC(K, "pm-server" | H(T))
mac_c  = HMAC(K, "pm-client" | H(T))
prk    = HMAC(K, ECDH(epk_c, epk_s))                   (HKDF-Extract, salt K)
okm    = HKDF-Expand(prk, "pm-keys" | H(T), 64)
k_c2s  = okm[0:32]     k_s2c = okm[32:64]
```

Both MACs are compared in constant time. Ephemeral ECDH keys give forward secrecy; the
pre-shared key authenticates both ends, so nobody without the pairing can connect, watch or
send input.

## Records

Each record is `len (u32) | AES-256-GCM(key, nonce, plaintext)` where the 12-byte nonce is
four zero bytes followed by a per-direction 64-bit counter starting at 0. Records larger than
8 MiB are rejected. A failed tag check closes the connection.

The plaintext is `type (u8) | payload`.

Phone to computer:

| type | name | payload |
|---|---|---|
| 0x01 | INFO | UTF-8 JSON: `name`, `model`, `android`, `control` (bool) |
| 0x02 | VIDEO_SESSION | width u32, height u32 (a new encoder starts, e.g. after rotation) |
| 0x03 | VIDEO_PACKET | flags u8 (1 = config, 2 = key frame), pts µs u64, H.264 data |
| 0x05 | STATUS | UTF-8 JSON: `control` (bool), optional `message` |

Computer to phone:

| type | name | payload |
|---|---|---|
| 0x10 | TOUCH | action u8 (0 down, 1 up, 2 move), x f32, y f32 (0..1 of the frame) |
| 0x11 | SCROLL | x f32, y f32, dx f32, dy f32 (wheel steps) |
| 0x12 | KEY | UTF-8 key name (`back`, `home`, `recents`, `enter`, ...) |
| 0x13 | TEXT | UTF-8 text to insert in the focused field |
| 0x14 | CLIPBOARD | paste u8, UTF-8 text |
