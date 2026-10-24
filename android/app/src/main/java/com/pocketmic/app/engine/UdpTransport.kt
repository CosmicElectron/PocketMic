package com.pocketmic.app.engine

import java.io.Closeable
import java.io.IOException
import java.net.InetSocketAddress
import java.net.SocketAddress
import java.net.StandardSocketOptions
import java.nio.ByteBuffer
import java.nio.channels.DatagramChannel
import java.nio.channels.SelectionKey
import java.nio.channels.Selector

/**
 * The single UDP socket [StreamEngine] listens and sends on. Sends never block: when a stalled
 * Wi-Fi link has filled the socket's send buffer, a datagram is dropped instead, so no thread
 * ever waits on the network while holding the engine's lock.
 */
interface Transport {
    val localPort: Int

    /** Queues the first [length] bytes of [data] for [to]; false if the send buffer was full. */
    fun send(data: ByteArray, length: Int, to: SocketAddress): Boolean

    /** Waits up to [timeoutMs] (> 0) for one datagram and copies it into [into]; null on timeout. */
    fun receive(into: ByteArray, timeoutMs: Long): Datagram?

    /** Releases the socket. Never throws. */
    fun close()
}

data class Datagram(val from: SocketAddress, val length: Int)

/** Opens the listening socket; replaceable so tests can inject failures. */
fun interface TransportOpener {
    fun open(address: InetSocketAddress): Transport
}

/** A broadcast-enabled, non-blocking [DatagramChannel]; receives wait on its own [Selector]. */
class UdpTransport private constructor(
    private val channel: DatagramChannel,
    private val selector: Selector,
) : Transport {
    override val localPort: Int = (channel.localAddress as InetSocketAddress).port

    override fun send(data: ByteArray, length: Int, to: SocketAddress): Boolean =
        channel.send(ByteBuffer.wrap(data, 0, length), to) > 0

    override fun receive(into: ByteArray, timeoutMs: Long): Datagram? {
        require(timeoutMs > 0) { "a zero timeout would wait forever" }
        val buffer = ByteBuffer.wrap(into)
        val from = channel.receive(buffer) ?: run {
            selector.select(timeoutMs)
            selector.selectedKeys().clear()
            channel.receive(buffer)
        }
        return from?.let { Datagram(it, buffer.position()) }
    }

    override fun close() {
        for (closeable in listOf<Closeable>(selector, channel)) {
            try {
                closeable.close()
            } catch (_: IOException) {
                // The descriptor is released even when close() reports an error; nothing to retry.
            }
        }
    }

    companion object {
        fun open(address: InetSocketAddress): UdpTransport {
            val channel = DatagramChannel.open()
            try {
                channel.setOption(StandardSocketOptions.SO_REUSEADDR, true)
                channel.setOption(StandardSocketOptions.SO_BROADCAST, true)
                channel.bind(address)
                channel.configureBlocking(false)
                val selector = Selector.open()
                try {
                    channel.register(selector, SelectionKey.OP_READ)
                } catch (e: IOException) {
                    selector.close()
                    throw e
                }
                return UdpTransport(channel, selector)
            } catch (e: IOException) {
                channel.close()
                throw e
            }
        }
    }
}
