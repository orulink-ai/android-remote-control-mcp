package com.danielealbano.androidremotecontrolmcp.services.workflow

import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedSelector
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflow
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflowEvent
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.nio.file.Files

class WorkflowJobQueueTest {
    private var now = 1_000L
    private var nextId = 0
    private val store = MemoryStore()

    private fun queue() = WorkflowJobQueue(store, clockMs = { now }, newId = { "job-${++nextId}" }, newToken = { "opaque-token-$nextId" })

    @Test
    fun `offline user submission persists before acknowledgement and raw is not in public state`() {
        val queue = queue()
        assertTrue(queue.submit(recording()))
        assertEquals(WorkflowJobStatus.QUEUED, store.value!!.job.status)
        assertEquals("recording-1", store.value!!.recording.id)
        assertFalse(queue.state.value.companionOnline)
        assertEquals(
            WorkflowJobStatus.QUEUED,
            queue.state.value.latestJob!!
                .status,
        )
        assertNull(
            queue.state.value.latestJob!!
                .summary,
        )
    }

    @Test
    fun `no request exists until local UI submits and polling cannot invent one`() {
        val queue = queue()
        assertNull(queue.claim("worker-a").job)
        assertNull(store.value)
        assertTrue(queue.state.value.companionOnline)
    }

    @Test
    fun `only one active job and duplicate submission never overwrites recording`() {
        val queue = queue()
        assertTrue(queue.submit(recording()))
        assertFalse(queue.submit(recording().copy(id = "recording-2")))
        assertEquals("recording-1", store.value!!.recording.id)
        assertEquals(WorkflowJobIssue.ACTIVE_JOB, queue.state.value.error)
    }

    @Test
    fun `invalid or empty or foreign recording never becomes queued`() {
        val queue = queue()
        for (raw in listOf(
            recording().copy(targetPackage = "other.app"),
            recording().copy(events = emptyList()),
            recording().copy(source = "unknown"),
        )) {
            assertFalse(queue.submit(raw))
            assertEquals(WorkflowJobIssue.INVALID_RECORDING, queue.state.value.error)
            assertNull(store.value)
        }
    }

    @Test
    fun `claim is atomic idempotent for owning worker and does not leak to competitors`() {
        val queue = queue()
        queue.submit(recording())
        val claim = queue.claim("worker-a")
        assertEquals(WorkflowJobStatus.VALIDATING, store.value!!.job.status)
        assertEquals("小狮日记", claim.job!!.task.query)
        assertEquals("generate_and_run", claim.job!!.operation)
        assertEquals(recording(), claim.job!!.recording)
        assertEquals(claim, queue.claim("worker-a"))
        assertNull(queue.claim("worker-b").job)
        assertNull(queue.claim("worker-b").claimToken)
    }

    @Test
    fun `generating and running polling cannot redispatch or extend the execution lease`() {
        for (status in listOf(WorkflowJobStatus.GENERATING, WorkflowJobStatus.RUNNING)) {
            store.value = null
            val queue = queue()
            queue.submit(recording())
            val claim = queue.claim("worker-a")
            queue.update(claim.job!!.id, claim.claimToken!!, WorkflowJobStatus.GENERATING)
            if (status == WorkflowJobStatus.RUNNING) queue.update(claim.job!!.id, claim.claimToken!!, status)
            val lease = store.value!!.leaseExpiresAtMs
            now += 80_000
            assertNull(queue.claim("worker-a").job, "$status must not redispatch")
            assertNull(queue.claim("worker-a").claimToken)
            assertEquals(lease, store.value!!.leaseExpiresAtMs, "$status polling must not renew the lease")
            now = lease!! + 1
            assertNull(queue.claim("worker-a").job)
            assertEquals(WorkflowJobStatus.FAILED, store.value!!.job.status)
            assertEquals(
                "WORKER_LEASE_EXPIRED",
                store.value!!
                    .job.summary!!
                    .errorCode,
            )
            assertNull(queue.claim("worker-b").job)
        }
    }

    @Test
    fun `explicit heartbeat renews leases while generating or running`() {
        for (status in listOf(WorkflowJobStatus.GENERATING, WorkflowJobStatus.RUNNING)) {
            store.value = null
            val queue = queue()
            queue.submit(recording())
            val claim = queue.claim("worker-a")
            queue.update(claim.job!!.id, claim.claimToken!!, WorkflowJobStatus.GENERATING)
            if (status == WorkflowJobStatus.RUNNING) queue.update(claim.job!!.id, claim.claimToken!!, status)
            val lease = store.value!!.leaseExpiresAtMs!!
            now += 80_000
            queue.heartbeat("worker-a", claim.job!!.id, claim.claimToken)
            assertTrue(store.value!!.leaseExpiresAtMs!! > lease)
            now = lease + 1
            queue.refresh()
            assertEquals(status, store.value!!.job.status)
        }
    }

    @Test
    fun `updates require matching claim and ordered statuses with idempotent repeats`() {
        val queue = queue()
        queue.submit(recording())
        val claim = queue.claim("worker-a")
        val id = claim.job!!.id
        val token = claim.claimToken!!
        assertThrows(WorkflowJobException::class.java) { queue.update(id, "wrong", WorkflowJobStatus.GENERATING) }
        assertThrows(WorkflowJobException::class.java) { queue.update(id, token, WorkflowJobStatus.SUCCEEDED) }
        queue.update(id, token, WorkflowJobStatus.VALIDATING)
        queue.update(id, token, WorkflowJobStatus.GENERATING)
        queue.update(id, token, WorkflowJobStatus.RUNNING)
        val summary = WorkflowJobSummary(piElapsedMs = 1234.0, modelCalls = 3, fallbackRequests = 0, artifactId = "workflow-result-1")
        val done = queue.update(id, token, WorkflowJobStatus.SUCCEEDED, summary)
        assertEquals(done, queue.update(id, token, WorkflowJobStatus.SUCCEEDED, summary))
        assertEquals(summary, store.value!!.job.summary)
        assertThrows(WorkflowJobException::class.java) { queue.update(id, token, WorkflowJobStatus.RUNNING) }
    }

    @Test
    fun `heartbeat refreshes presence and active lease only with correct token`() {
        val queue = queue()
        queue.submit(recording())
        val claim = queue.claim("worker-a")
        now += 80_000
        queue.heartbeat("worker-a", claim.job!!.id, claim.claimToken)
        now += 20_000
        queue.refresh()
        assertTrue(queue.state.value.companionOnline)
        assertTrue(
            queue.state.value.latestJob!!
                .status.isActive,
        )
        assertThrows(WorkflowJobException::class.java) { queue.heartbeat("worker-a", claim.job!!.id, "wrong") }
        now += 91_000
        queue.refresh()
        assertEquals(
            WorkflowJobStatus.FAILED,
            queue.state.value.latestJob!!
                .status,
        )
        assertEquals(
            "WORKER_LEASE_EXPIRED",
            queue.state.value.latestJob!!
                .summary!!
                .errorCode,
        )
        assertNull(queue.claim("worker-b").job)
    }

    @Test
    fun `presence expires but queued job remains queued for later explicit claim`() {
        val queue = queue()
        queue.submit(recording())
        queue.heartbeat("worker-a")
        now += 31_000
        queue.refresh()
        assertFalse(queue.state.value.companionOnline)
        assertEquals(
            WorkflowJobStatus.QUEUED,
            queue.state.value.latestJob!!
                .status,
        )
    }

    @Test
    fun `queued cancellation persists but cannot cancel claimed execution`() {
        val queue = queue()
        queue.submit(recording())
        assertTrue(queue.cancelQueued())
        assertEquals(WorkflowJobStatus.CANCELLED, store.value!!.job.status)
        assertNull(queue.claim("worker-a").job)
        queue.submit(recording())
        queue.claim("worker-a")
        assertFalse(queue.cancelQueued())
        assertEquals(WorkflowJobStatus.VALIDATING, store.value!!.job.status)
    }

    @Test
    fun `process restart preserves queued and fails claimed job without replay`() {
        val queue = queue()
        queue.submit(recording())
        assertEquals(
            WorkflowJobStatus.QUEUED,
            queue()
                .state.value.latestJob!!
                .status,
        )
        queue.claim("worker-a")
        val restored = queue()
        assertEquals(
            WorkflowJobStatus.FAILED,
            restored.state.value.latestJob!!
                .status,
        )
        assertEquals(
            "PROCESS_RESTARTED",
            restored.state.value.latestJob!!
                .summary!!
                .errorCode,
        )
        assertNull(restored.claim("worker-a").job)
    }

    @Test
    fun `storage failure cannot acknowledge queued or updated job`() {
        val queue = queue()
        store.failSave = true
        assertFalse(queue.submit(recording()))
        assertNull(queue.state.value.latestJob)
        assertEquals(WorkflowJobIssue.STORAGE_FAILURE, queue.state.value.error)
        store.failSave = false
        assertTrue(queue.submit(recording()))
        val claim = queue.claim("worker-a")
        store.failSave = true
        assertThrows(WorkflowJobException::class.java) { queue.update(claim.job!!.id, claim.claimToken!!, WorkflowJobStatus.GENERATING) }
        assertEquals(
            WorkflowJobStatus.VALIDATING,
            queue.state.value.latestJob!!
                .status,
        )
    }

    @Test
    fun `bounded summary rejects paths secrets and nonfinite metrics before persistence`() {
        val queue = queue()
        queue.submit(recording())
        val claim = queue.claim("worker-a")
        for (summary in listOf(
            WorkflowJobSummary(piElapsedMs = Double.NaN),
            WorkflowJobSummary(modelCalls = -1),
            WorkflowJobSummary(artifactId = "/Users/private/result"),
            WorkflowJobSummary(artifactId = "sk-or-v1-private"),
            WorkflowJobSummary(errorCode = "private path/secret"),
        )) {
            assertThrows(
                WorkflowJobException::class.java,
            ) { queue.update(claim.job!!.id, claim.claimToken!!, WorkflowJobStatus.GENERATING, summary) }
        }
        assertEquals(WorkflowJobStatus.VALIDATING, store.value!!.job.status)
    }

    @Test
    fun `file persistence roundtrips private job and rejects corrupt state`() {
        val directory = Files.createTempDirectory("workflow-jobs-test").toFile()
        try {
            val disk = WorkflowJobFileStore(directory)
            val queue = WorkflowJobQueue(disk, clockMs = { now })
            assertTrue(queue.submit(recording()))
            assertEquals(recording(), disk.load()!!.recording)
            assertEquals(
                WorkflowJobStatus.QUEUED,
                WorkflowJobQueue(disk)
                    .state.value.latestJob!!
                    .status,
            )
            directory.resolve("latest.json").writeText("{broken")
            val corrupt = WorkflowJobQueue(disk)
            assertEquals(WorkflowJobIssue.STORAGE_FAILURE, corrupt.state.value.error)
            assertFalse(corrupt.submit(recording()))
        } finally {
            directory.deleteRecursively()
        }
    }

    private class MemoryStore : WorkflowJobStore {
        var value: StoredWorkflowJob? = null
        var failSave = false

        override fun load() = value

        override fun save(job: StoredWorkflowJob) {
            if (failSave) error("disk unavailable")
            value = job
        }
    }

    private fun recording() =
        RecordedWorkflow(
            "recording-1",
            "小红书演示",
            "com.xingin.xhs",
            100,
            200,
            listOf(RecordedWorkflowEvent(1, 10, "text", RecordedSelector("com.xingin.xhs", "EditText", "search"), "小狮日记")),
        )
}
