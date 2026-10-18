package io.github.nanorex.phonemirror

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.Closeable
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.IOException
import java.net.Socket
import java.nio.ByteBuffer
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Phone side of the phonemirror link (docs/protocol.md): a pre-shared-key authenticated
 * ECDH handshake, then AES-256-GCM records with counter nonces. Pure JVM code so it can be
 * unit tested and checked against the Python implementation.
 */
class SecureLink private constructor(
    private val socket: Socket,
    sendKey: ByteArray,
    recvKey: ByteArray,
) : Closeable {
    private val input = DataInputStream(BufferedInputStream(socket.getInputStream(), 65536))
    private val output = DataOutputStream(BufferedOutputStream(socket.getOutputStream(), 65536))
    private val sendKey = SecretKeySpec(sendKey, "AES")
    private val recvKey = SecretKeySpec(recvKey, "AES")
    private var sendCounter = 0L
    private var recvCounter = 0L
    private val sendLock = Any()

    fun send(plaintext: ByteArray) {
        synchronized(sendLock) {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, sendKey, GCMParameterSpec(128, nonce(sendCounter)))
            sendCounter++
            val ct = cipher.doFinal(plaintext)
            output.writeInt(ct.size)
            output.write(ct)
            output.flush()
        }
    }

    /** Reads one record; only one thread may call this. */
    fun recv(): ByteArray {
        val length = input.readInt()
        if (length < 16 || length > MAX_RECORD) throw IOException("bad record length $length")
        val ct = ByteArray(length)
        input.readFully(ct)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, recvKey, GCMParameterSpec(128, nonce(recvCounter)))
        val pt = try {
            cipher.doFinal(ct)
        } catch (e: Exception) {
            throw IOException("record authentication failed")
        }
        recvCounter++
        return pt
    }

    override fun close() {
        try {
            socket.close()
        } catch (_: IOException) {
        }
    }

    companion object {
        private val MAGIC = "PMIR".toByteArray(Charsets.US_ASCII)
        private const val VERSION = 1
        const val MAX_RECORD = 8 * 1024 * 1024
        private const val MAX_KEY_DER = 512

        fun nonce(counter: Long): ByteArray =
            ByteBuffer.allocate(12).putInt(0).putLong(counter).array()

        fun hmac(key: ByteArray, vararg parts: ByteArray): ByteArray {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(key, "HmacSHA256"))
            for (p in parts) mac.update(p)
            return mac.doFinal()
        }

        fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

        fun hkdfExpand(prk: ByteArray, info: ByteArray, length: Int): ByteArray {
            val out = java.io.ByteArrayOutputStream()
            var block = ByteArray(0)
            var i = 1
            while (out.size() < length) {
                block = hmac(prk, block, info, byteArrayOf(i.toByte()))
                out.write(block)
                i++
            }
            return out.toByteArray().copyOf(length)
        }

        /** Returns (client-to-server key, server-to-client key). */
        fun deriveKeys(psk: ByteArray, shared: ByteArray, transcript: ByteArray): Pair<ByteArray, ByteArray> {
            val prk = hmac(psk, shared)
            val okm = hkdfExpand(prk, "pm-keys".toByteArray() + sha256(transcript), 64)
            return Pair(okm.copyOfRange(0, 32), okm.copyOfRange(32, 64))
        }

        fun transcriptMac(psk: ByteArray, label: String, transcript: ByteArray): ByteArray =
            hmac(psk, label.toByteArray(Charsets.US_ASCII), sha256(transcript))

        /** Runs the client handshake on a connected socket. */
        fun connect(socket: Socket, pairId: ByteArray, psk: ByteArray): SecureLink {
            require(pairId.size == 16 && psk.size == 32)
            val input = DataInputStream(socket.getInputStream())
            val output = socket.getOutputStream()

            val kpg = KeyPairGenerator.getInstance("EC")
            kpg.initialize(ECGenParameterSpec("secp256r1"))
            val keyPair = kpg.generateKeyPair()
            val epkC = keyPair.public.encoded
            val nonceC = ByteArray(32).also { SecureRandom().nextBytes(it) }
            val clientHello = MAGIC + byteArrayOf(VERSION.toByte()) + pairId + nonceC +
                ByteBuffer.allocate(2).putShort(epkC.size.toShort()).array() + epkC
            output.write(clientHello)
            output.flush()

            val head = ByteArray(4 + 1 + 32 + 2)
            input.readFully(head)
            if (!head.copyOfRange(0, 4).contentEquals(MAGIC) || head[4].toInt() != VERSION) {
                throw IOException("not a phonemirror computer")
            }
            val keyLength = ByteBuffer.wrap(head, 37, 2).short.toInt() and 0xFFFF
            if (keyLength == 0 || keyLength > MAX_KEY_DER) throw IOException("bad key length")
            val epkS = ByteArray(keyLength)
            input.readFully(epkS)
            val transcript = clientHello + head + epkS
            val macS = ByteArray(32)
            input.readFully(macS)
            if (!MessageDigest.isEqual(macS, transcriptMac(psk, "pm-server", transcript))) {
                throw IOException("the computer did not prove the pairing key; pair again")
            }
            output.write(transcriptMac(psk, "pm-client", transcript))
            output.flush()

            val peer = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(epkS))
            val ours = keyPair.public as ECPublicKey
            if (peer !is ECPublicKey || peer.params.curve != ours.params.curve) {
                throw IOException("the computer's key is not P-256")
            }
            val agreement = KeyAgreement.getInstance("ECDH")
            agreement.init(keyPair.private)
            agreement.doPhase(peer, true)
            val (c2s, s2c) = deriveKeys(psk, agreement.generateSecret(), transcript)
            return SecureLink(socket, c2s, s2c)
        }
    }
}
