package com.danielealbano.androidremotecontrolmcp.services.screencapture

import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Serializes accessibility screenshot requests; minimum interval is enforced at dispatch. */
class ScreenshotRequestGate(
    private val minIntervalMs: Long = MIN_INTERVAL_MS,
    private val clockMs: () -> Long = { System.nanoTime() / NANOS_PER_MILLISECOND },
    private val suspendDelay: suspend (Long) -> Unit = { delay(it) },
) {
    private val mutex = Mutex()
    private var lastDispatchMs: Long? = null

    init {
        require(minIntervalMs > 0) { "Screenshot interval must be positive" }
    }

    suspend fun <T> capture(block: suspend () -> T): T =
        mutex.withLock {
            currentCoroutineContext().ensureActive()
            lastDispatchMs?.let { previous ->
                var remaining = minIntervalMs - (clockMs() - previous)
                while (remaining > 0) {
                    suspendDelay(remaining)
                    currentCoroutineContext().ensureActive()
                    remaining = minIntervalMs - (clockMs() - previous)
                }
            }
            currentCoroutineContext().ensureActive()
            // Reserve only when dispatching, not while a cancellable waiter is queued.
            // Failed native requests also count; the platform has still seen a call.
            lastDispatchMs = clockMs()
            block()
        }

    companion object {
        // AOSP's ACCESSIBILITY_TAKE_SCREENSHOT_REQUEST_INTERVAL_TIMES_MS is 333;
        // 17 ms margin avoids the <=333 rejection boundary. No retry is implied.
        const val MIN_INTERVAL_MS = 350L
        private const val NANOS_PER_MILLISECOND = 1_000_000L
    }
}
