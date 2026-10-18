package io.github.nanorex.phonemirror

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.net.Socket
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Vectors are shared with tests/test_link.py so both implementations agree byte for byte. */
class SecureLinkTest {
    private fun hex(s: String) = Pairing.hexToBytes(s)
    private val psk = ByteArray(32) { it.toByte() }

    @Test
    fun keyDerivationMatchesPython() {
        val (c2s, s2c) = SecureLink.deriveKeys(psk, ByteArray(32) { 7 }, "transcript".toByteArray())
        assertEquals("344d3c27e2d2c2c4ac9078491538aaea3bf2d35f970d910e2b282d8f44c56276", Pairing.bytesToHex(c2s))
        assertEquals("3e7ad84fc4300481b22bab21ba15a9330fa6495c45f23195285cf06972315d41", Pairing.bytesToHex(s2c))
    }

    @Test
    fun transcriptMacMatchesPython() {
        assertEquals(
            "c09a9b9d9d69266e0fbbace5d19b3e6ebe917d4fa3ff3830aeb0fd1dd0858d19",
            Pairing.bytesToHex(SecureLink.transcriptMac(psk, "pm-server", "transcript".toByteArray())),
        )
    }

    @Test
    fun recordEncryptionMatchesPython() {
        val (c2s, _) = SecureLink.deriveKeys(psk, ByteArray(32) { 7 }, "transcript".toByteArray())
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(c2s, "AES"), GCMParameterSpec(128, SecureLink.nonce(5)))
        assertArrayEquals(hex("3011b9a7ef30011b5362522486ef30dcac12543397"), cipher.doFinal("hello".toByteArray()))
    }

    @Test
    fun hkdfMatchesRfc5869() {
        val prk = SecureLink.hmac(ByteArray(13) { it.toByte() }, ByteArray(22) { 0x0b })
        val okm = SecureLink.hkdfExpand(prk, ByteArray(10) { (0xf0 + it).toByte() }, 42)
        assertEquals(
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865",
            Pairing.bytesToHex(okm),
        )
    }

    @Test
    fun pairingUriParses() {
        val p = Pairing.parse(
            "phonemirror://pair?v=1&id=000102030405060708090a0b0c0d0e0f&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8" +
                "&pc=abababababababababababababababab&n=My+PC+%26+co&h=192.168.1.5%2C10.0.0.2&p=27199"
        )
        assertEquals("My PC & co", p.pcName)
        assertEquals(listOf("192.168.1.5", "10.0.0.2"), p.hosts)
        assertArrayEquals(psk, p.key)
        assertThrows(IllegalArgumentException::class.java) { Pairing.parse("https://example.com/?v=1") }
        assertThrows(IllegalArgumentException::class.java) { Pairing.parse("phonemirror://pair?v=1&id=zz") }
    }

    /** Full handshake against the Python computer side (CI starts tests/interop_server.py). */
    @Test
    fun interopWithPythonServer() {
        val port = System.getenv("PM_INTEROP_PORT")?.toIntOrNull()
        assumeTrue(port != null)
        Socket("127.0.0.1", port!!).use { socket ->
            val link = SecureLink.connect(socket, ByteArray(16) { it.toByte() }, psk)
            link.send("hello from kotlin".toByteArray())
            assertEquals("echo:hello from kotlin", String(link.recv()))
        }
    }
}
