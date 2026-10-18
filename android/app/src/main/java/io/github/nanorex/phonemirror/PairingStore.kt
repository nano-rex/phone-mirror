package io.github.nanorex.phonemirror

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Keeps the single computer pairing in app-private preferences. The pairing key is
 * encrypted with a non-exportable Android Keystore key, and backups are disabled in the
 * manifest, so it never leaves the phone.
 */
class PairingStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("pairing", Context.MODE_PRIVATE)

    fun load(): Pairing? {
        val id = prefs.getString("id", null) ?: return null
        val sealed = prefs.getString("key", null) ?: return null
        val key = try {
            unseal(Base64.decode(sealed, Base64.NO_WRAP))
        } catch (e: Exception) {
            return null // keystore key lost (e.g. device restored): pair again
        }
        return Pairing(
            Pairing.hexToBytes(id), key,
            prefs.getString("pc", "") ?: "",
            prefs.getString("name", "computer") ?: "computer",
            (prefs.getString("hosts", "") ?: "").split(",").filter { it.isNotEmpty() },
            prefs.getInt("port", Protocol.DEFAULT_PORT),
        )
    }

    fun save(p: Pairing) {
        prefs.edit()
            .putString("id", Pairing.bytesToHex(p.pairId))
            .putString("key", Base64.encodeToString(seal(p.key), Base64.NO_WRAP))
            .putString("pc", p.pcId)
            .putString("name", p.pcName)
            .putString("hosts", p.hosts.joinToString(","))
            .putInt("port", p.port)
            .apply()
    }

    /** Remember where the computer was last found, so the next connection is quick. */
    fun updateAddress(host: String, port: Int) {
        val current = load() ?: return
        val hosts = (listOf(host) + current.hosts.filter { it != host }).take(8)
        prefs.edit().putString("hosts", hosts.joinToString(",")).putInt("port", port).apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
    }

    private fun secretKey(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    private fun seal(plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        return cipher.iv + cipher.doFinal(plain)
    }

    private fun unseal(data: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, data.copyOfRange(0, 12)))
        return cipher.doFinal(data, 12, data.size - 12)
    }

    companion object {
        private const val ALIAS = "phonemirror-pairing"
    }
}
