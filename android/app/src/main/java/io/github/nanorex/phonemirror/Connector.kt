package io.github.nanorex.phonemirror

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** Finds the paired computer on the local network and opens a TCP connection to it. */
class Connector(private val context: Context, private val store: PairingStore) {

    fun connect(pairing: Pairing): Socket {
        for (host in pairing.hosts) {
            tryConnect(host, pairing.port)?.let { return it }
        }
        val found = discover(pairing.pcId, 8000)
            ?: throw IOException(
                "Couldn't find ${pairing.pcName}. Make sure phonemirror is open on the computer " +
                    "and both are on the same Wi-Fi."
            )
        val socket = tryConnect(found.first, found.second)
            ?: throw IOException("Found ${pairing.pcName} but couldn't connect. Check its firewall.")
        store.updateAddress(found.first, found.second)
        return socket
    }

    private fun tryConnect(host: String, port: Int): Socket? {
        val socket = Socket()
        return try {
            socket.connect(InetSocketAddress(host, port), 1500)
            socket.tcpNoDelay = true
            socket
        } catch (e: IOException) {
            socket.close()
            null
        }
    }

    /** mDNS discovery of _phonemirror._tcp, matching the computer id in the TXT record. */
    @Suppress("DEPRECATION") // resolveService is deprecated on 34+ but still works everywhere
    private fun discover(pcId: String, timeoutMs: Long): Pair<String, Int>? {
        val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        val candidates = LinkedBlockingQueue<NsdServiceInfo>()
        val listener = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(info: NsdServiceInfo) {
                candidates.offer(info)
            }
            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onDiscoveryStarted(type: String) {}
            override fun onDiscoveryStopped(type: String) {}
            override fun onStartDiscoveryFailed(type: String, code: Int) {}
            override fun onStopDiscoveryFailed(type: String, code: Int) {}
        }
        try {
            nsd.discoverServices(Protocol.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            return null
        }
        val deadline = System.currentTimeMillis() + timeoutMs
        try {
            while (true) {
                val left = deadline - System.currentTimeMillis()
                if (left <= 0) return null
                val candidate = candidates.poll(left, TimeUnit.MILLISECONDS) ?: return null
                // one resolve at a time: older Android versions reject concurrent resolves
                val done = CountDownLatch(1)
                var resolved: NsdServiceInfo? = null
                nsd.resolveService(candidate, object : NsdManager.ResolveListener {
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        resolved = info
                        done.countDown()
                    }
                    override fun onResolveFailed(info: NsdServiceInfo, code: Int) {
                        done.countDown()
                    }
                })
                done.await(3, TimeUnit.SECONDS)
                val info = resolved ?: continue
                val id = info.attributes["pc"]?.toString(Charsets.UTF_8)
                val host = info.host?.hostAddress
                if (id == pcId && host != null) return Pair(host, info.port)
            }
        } finally {
            try {
                nsd.stopServiceDiscovery(listener)
            } catch (_: Exception) {
            }
        }
    }
}
