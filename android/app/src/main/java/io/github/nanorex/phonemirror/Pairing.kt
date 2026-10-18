package io.github.nanorex.phonemirror

import java.net.URI
import java.net.URLDecoder
import java.util.Base64

/** A pairing with one computer, created by scanning its QR code. */
data class Pairing(
    val pairId: ByteArray,
    val key: ByteArray,
    val pcId: String,
    val pcName: String,
    val hosts: List<String>,
    val port: Int,
) {
    companion object {
        private val HEX = Regex("^[0-9a-f]{32}$")
        private val HOST = Regex("^[0-9A-Za-z.:%-]{1,64}$")

        /** Parses phonemirror://pair?... ; throws IllegalArgumentException when malformed. */
        fun parse(text: String): Pairing {
            val uri = try {
                URI(text.trim())
            } catch (e: Exception) {
                throw IllegalArgumentException("This is not a phonemirror code.")
            }
            require(uri.scheme == "phonemirror" && uri.host == "pair") { "This is not a phonemirror code." }
            val q = HashMap<String, String>()
            for (part in (uri.rawQuery ?: "").split("&")) {
                val eq = part.indexOf('=')
                if (eq <= 0) continue
                q[URLDecoder.decode(part.substring(0, eq), "UTF-8")] =
                    URLDecoder.decode(part.substring(eq + 1), "UTF-8")
            }
            require(q["v"] == "1") { "This code needs a newer version of the app." }
            val id = q["id"].orEmpty()
            val pc = q["pc"].orEmpty()
            require(HEX.matches(id) && HEX.matches(pc)) { "The code is damaged. Try again." }
            val key = try {
                Base64.getUrlDecoder().decode(q["k"].orEmpty())
            } catch (e: IllegalArgumentException) {
                ByteArray(0)
            }
            require(key.size == 32) { "The code is damaged. Try again." }
            val port = q["p"]?.toIntOrNull() ?: Protocol.DEFAULT_PORT
            require(port in 1..65535) { "The code is damaged. Try again." }
            val hosts = q["h"].orEmpty().split(",").filter { HOST.matches(it) }.take(8)
            val name = q["n"].orEmpty().filter { !it.isISOControl() }.take(64).ifEmpty { "computer" }
            return Pairing(hexToBytes(id), key, pc, name, hosts, port)
        }

        fun hexToBytes(hex: String): ByteArray =
            ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

        fun bytesToHex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }
    }

    override fun equals(other: Any?): Boolean =
        other is Pairing && pairId.contentEquals(other.pairId) && key.contentEquals(other.key) &&
            pcId == other.pcId && pcName == other.pcName && hosts == other.hosts && port == other.port

    override fun hashCode(): Int = pairId.contentHashCode()
}
