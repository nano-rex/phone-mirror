package io.github.nanorex.phonemirror

/** Message types of the phonemirror link (docs/protocol.md). */
object Protocol {
    const val DEFAULT_PORT = 27199
    const val SERVICE_TYPE = "_phonemirror._tcp"

    // phone -> computer
    const val MSG_INFO: Byte = 0x01
    const val MSG_VIDEO_SESSION: Byte = 0x02
    const val MSG_VIDEO_PACKET: Byte = 0x03
    const val MSG_STATUS: Byte = 0x05

    // computer -> phone
    const val MSG_TOUCH: Byte = 0x10
    const val MSG_SCROLL: Byte = 0x11
    const val MSG_KEY: Byte = 0x12
    const val MSG_TEXT: Byte = 0x13
    const val MSG_CLIPBOARD: Byte = 0x14

    const val FLAG_CONFIG = 1
    const val FLAG_KEY = 2

    const val TOUCH_DOWN = 0
    const val TOUCH_UP = 1
    const val TOUCH_MOVE = 2
}
