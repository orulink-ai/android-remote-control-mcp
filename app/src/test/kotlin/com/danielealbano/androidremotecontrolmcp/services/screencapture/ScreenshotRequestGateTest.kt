package com.danielealbano.androidremotecontrolmcp.services.screencapture

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ScreenshotRequestGateTest {
    @Test
    fun `first request captures immediately without an artificial delay`() =
        runTest {
            val waits = mutableListOf<Long>()
            val gate =
                ScreenshotRequestGate(clockMs = { testScheduler.currentTime }, suspendDelay = {
                    waits += it
                    delay(it)
                })
            assertEquals("frame", gate.capture { "frame" })
            assertEquals(emptyList<Long>(), waits)
            assertEquals(0L, testScheduler.currentTime)
        }

    @Test
    fun `rapid sequential requests respect minimum interval from dispatch`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            val starts = mutableListOf<Long>()
            repeat(3) {
                gate.capture {
                    starts += testScheduler.currentTime
                    delay(50)
                }
            }
            assertEquals(listOf(0L, 350L, 700L), starts)
        }

    @Test
    fun `simultaneous requests are serialized and dispatched at least 350ms apart`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            val starts = mutableListOf<Long>()
            var running = 0
            val jobs =
                List(4) {
                    async {
                        gate.capture {
                            starts += testScheduler.currentTime
                            running++
                            assertEquals(1, running)
                            delay(100)
                            running--
                        }
                    }
                }
            jobs.forEach { it.await() }
            assertEquals(listOf(0L, 350L, 700L, 1050L), starts)
        }

    @Test
    fun `slow previous capture releases next without redundant interval delay`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            gate.capture { delay(500) }
            gate.capture { assertEquals(500L, testScheduler.currentTime) }
        }

    @Test
    fun `cancelling request queued behind capture never dispatches it`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            val release = CompletableDeferred<Unit>()
            val first = launch { gate.capture { release.await() } }
            runCurrent()
            var cancelledCapture = false
            val queued = launch { gate.capture { cancelledCapture = true } }
            runCurrent()
            queued.cancelAndJoin()
            release.complete(Unit)
            first.join()
            gate.capture { assertFalse(cancelledCapture) }
        }

    @Test
    fun `cancelling interval wait neither captures nor consumes another slot`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            gate.capture { }
            var cancelledCapture = false
            val queued = launch { gate.capture { cancelledCapture = true } }
            runCurrent()
            advanceTimeBy(100)
            queued.cancelAndJoin()
            gate.capture { assertEquals(350L, testScheduler.currentTime) }
            assertFalse(cancelledCapture)
        }

    @Test
    fun `capture failure propagates without retry and releases mutex`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            var attempts = 0
            val result =
                runCatching {
                    gate.capture {
                        attempts++
                        error("non-interval platform error")
                    }
                }
            assertTrue(result.exceptionOrNull() is IllegalStateException)
            assertEquals(1, attempts)
            gate.capture { attempts++ }
            assertEquals(2, attempts)
            assertEquals(350L, testScheduler.currentTime)
        }

    @Test
    fun `caller timeout includes interval waiting and prevents dispatch`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            gate.capture { }
            var captured = false
            val value =
                withTimeoutOrNull(100) {
                    gate.capture {
                        captured = true
                        "unexpected"
                    }
                }
            assertEquals(null, value)
            assertEquals(100L, testScheduler.currentTime)
            assertFalse(captured)
            gate.capture { assertEquals(350L, testScheduler.currentTime) }
        }

    @Test
    fun `caller timeout includes mutex queue and does not block future requests`() =
        runTest {
            val gate = ScreenshotRequestGate(clockMs = { testScheduler.currentTime })
            val release = CompletableDeferred<Unit>()
            val first = launch { gate.capture { release.await() } }
            runCurrent()
            var captured = false
            assertEquals(null, withTimeoutOrNull(100) { gate.capture { captured = true } })
            assertFalse(captured)
            release.complete(Unit)
            first.join()
            gate.capture { assertEquals(350L, testScheduler.currentTime) }
        }

    @Test
    fun `early delay completion is checked again before dispatch`() =
        runTest {
            var now = 0L
            val waits = mutableListOf<Long>()
            val gate =
                ScreenshotRequestGate(clockMs = { now }, suspendDelay = {
                    waits += it
                    now += if (waits.size == 1) it - 1 else it
                })
            gate.capture { }
            gate.capture { assertEquals(350L, now) }
            assertEquals(listOf(350L, 1L), waits)
        }

    @Test
    fun `zero interval configuration is rejected`() {
        assertThrows(IllegalArgumentException::class.java) { ScreenshotRequestGate(minIntervalMs = 0) }
    }
}
