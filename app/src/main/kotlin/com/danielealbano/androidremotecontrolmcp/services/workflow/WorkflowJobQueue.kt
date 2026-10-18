package com.danielealbano.androidremotecontrolmcp.services.workflow

import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflow
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingSession
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.UUID

/** One synchronized job slot. Every state-changing acknowledgement follows a successful save. */
@Suppress("TooManyFunctions")
class WorkflowJobQueue(
    private val store: WorkflowJobStore,
    private val clockMs: () -> Long = System::currentTimeMillis,
    private val newId: () -> String = { UUID.randomUUID().toString() },
    private val newToken: () -> String = { UUID.randomUUID().toString() },
    private val leaseClockMs: () -> Long = clockMs,
) {
    private val mutableState = MutableStateFlow(WorkflowJobUiState())
    val state: StateFlow<WorkflowJobUiState> = mutableState.asStateFlow()
    private var current: StoredWorkflowJob? = null
    private var presenceExpiresAt: Long? = null
    private var loadFailed = false

    init {
        runCatching {
            store.load()?.let { saved ->
                check(validRecording(saved.recording) && saved.schemaVersion == 1 && saved.job.recordingId == saved.recording.id)
                current = saved
                if (saved.job.status.isActive && saved.job.status != WorkflowJobStatus.QUEUED) {
                    persist(failed(saved, "PROCESS_RESTARTED"))
                } else {
                    publish()
                }
            }
        }.onFailure {
            loadFailed = true
            current = null
            mutableState.value = WorkflowJobUiState(error = WorkflowJobIssue.STORAGE_FAILURE)
        }
    }

    @Synchronized
    fun submit(recording: RecordedWorkflow): Boolean {
        if (loadFailed) return reject(WorkflowJobIssue.STORAGE_FAILURE)
        refresh()
        if (current?.job?.status?.isActive == true) return reject(WorkflowJobIssue.ACTIVE_JOB)
        if (!validRecording(recording)) return reject(WorkflowJobIssue.INVALID_RECORDING)
        val now = clockMs()
        val stored =
            StoredWorkflowJob(
                job = WorkflowJob(newId(), recording.id, recording.name, WorkflowJobStatus.QUEUED, now, now),
                recording = recording,
            )
        return try {
            persist(stored)
            true
        } catch (_: WorkflowJobException) {
            false
        }
    }

    @Synchronized
    fun cancelQueued(): Boolean {
        val value = current ?: return false
        if (value.job.status != WorkflowJobStatus.QUEUED) return false
        return try {
            persist(value.copy(job = value.job.copy(status = WorkflowJobStatus.CANCELLED, updatedAtEpochMs = clockMs())))
            true
        } catch (_: WorkflowJobException) {
            false
        }
    }

    @Synchronized
    fun claim(workerId: String): WorkflowClaim {
        requireWorker(workerId)
        refresh()
        val value = current
        if (value == null || !value.job.status.isActive) {
            markPresence()
            return WorkflowClaim()
        }
        if (value.job.status == WorkflowJobStatus.QUEUED) {
            val claimed =
                value.copy(
                    job = value.job.copy(status = WorkflowJobStatus.VALIDATING, updatedAtEpochMs = clockMs()),
                    claimedBy = workerId,
                    claimToken = newToken(),
                    leaseExpiresAtMs = leaseClockMs() + LEASE_MS,
                )
            persist(claimed)
        } else if (value.claimedBy == workerId && value.job.status == WorkflowJobStatus.VALIDATING) {
            persist(value.copy(leaseExpiresAtMs = leaseClockMs() + LEASE_MS))
        } else {
            markPresence()
            return WorkflowClaim()
        }
        markPresence()
        val claimed = requireNotNull(current)
        return WorkflowClaim(
            WorkflowClaimJob(claimed.job.id, claimed.job.recordingId, claimed.job.name, claimed.recording),
            claimed.claimToken,
        )
    }

    @Synchronized
    fun heartbeat(
        workerId: String,
        jobId: String? = null,
        claimToken: String? = null,
    ): WorkflowJobUiState {
        requireWorker(workerId)
        refresh()
        if ((jobId == null) != (claimToken == null)) throw WorkflowJobException("INVALID_CLAIM")
        if (jobId != null && claimToken != null) {
            val value = requireClaim(jobId, claimToken)
            if (value.claimedBy != workerId) throw WorkflowJobException("INVALID_CLAIM")
            if (value.job.status.isActive) persist(value.copy(leaseExpiresAtMs = leaseClockMs() + LEASE_MS))
        }
        markPresence()
        return state.value
    }

    @Synchronized
    fun update(
        jobId: String,
        claimToken: String,
        status: WorkflowJobStatus,
        summary: WorkflowJobSummary? = null,
    ): WorkflowJob {
        refresh()
        if (!validSummary(summary)) throw WorkflowJobException("INVALID_SUMMARY")
        val value = requireClaim(jobId, claimToken)
        val previous = value.job.status
        if (!previous.isActive) {
            if (status == previous) return value.job
            throw WorkflowJobException("INVALID_TRANSITION")
        }
        val next =
            when (previous) {
                WorkflowJobStatus.VALIDATING -> WorkflowJobStatus.GENERATING
                WorkflowJobStatus.GENERATING -> WorkflowJobStatus.RUNNING
                WorkflowJobStatus.RUNNING -> WorkflowJobStatus.SUCCEEDED
                else -> null
            }
        if (status != previous && status != next && status != WorkflowJobStatus.FAILED) throw WorkflowJobException("INVALID_TRANSITION")
        val updated =
            value.copy(
                job = value.job.copy(status = status, updatedAtEpochMs = clockMs(), summary = merge(value.job.summary, summary)),
                leaseExpiresAtMs = leaseClockMs() + LEASE_MS,
            )
        persist(updated)
        markPresence()
        return updated.job
    }

    @Synchronized
    fun refresh() {
        val value = current
        if (value != null && value.job.status.isActive && value.job.status != WorkflowJobStatus.QUEUED) {
            val expiry = value.leaseExpiresAtMs
            if (expiry == null || leaseClockMs() >= expiry) {
                persist(failed(value, "WORKER_LEASE_EXPIRED"))
            }
        }
        publish()
    }

    private fun failed(
        value: StoredWorkflowJob,
        code: String,
    ): StoredWorkflowJob =
        value.copy(
            job =
                value.job.copy(
                    status = WorkflowJobStatus.FAILED,
                    updatedAtEpochMs = clockMs(),
                    summary = merge(value.job.summary, WorkflowJobSummary(errorCode = code)),
                ),
        )

    private fun requireClaim(
        id: String,
        token: String,
    ): StoredWorkflowJob {
        val value = current ?: throw WorkflowJobException("INVALID_CLAIM")
        if (value.job.id != id || token.isEmpty() || value.claimToken != token) throw WorkflowJobException("INVALID_CLAIM")
        return value
    }

    private fun requireWorker(worker: String) {
        if (!SAFE_ID.matches(worker) || worker.length > 64) throw WorkflowJobException("INVALID_WORKER")
    }

    private fun markPresence() {
        presenceExpiresAt = leaseClockMs() + PRESENCE_MS
        publish()
    }

    private fun publish(error: WorkflowJobIssue? = state.value.error) {
        mutableState.value = WorkflowJobUiState(current?.job, presenceExpiresAt?.let { leaseClockMs() < it } ?: false, error)
    }

    private fun persist(value: StoredWorkflowJob) {
        if (loadFailed) throw WorkflowJobException("STORAGE_FAILURE")
        try {
            store.save(value)
        } catch (_: Exception) {
            mutableState.value = state.value.copy(error = WorkflowJobIssue.STORAGE_FAILURE)
            throw WorkflowJobException("STORAGE_FAILURE")
        }
        current = value
        publish(error = null)
    }

    @Synchronized
    fun reportInvalidRecording(): Boolean = reject(WorkflowJobIssue.INVALID_RECORDING)

    private fun reject(issue: WorkflowJobIssue): Boolean {
        mutableState.value = state.value.copy(error = issue)
        return false
    }

    companion object {
        const val LEASE_MS = 90_000L
        const val PRESENCE_MS = 30_000L
        const val HEARTBEAT_SECONDS = 10
        const val LEASE_SECONDS = 90
        private val SAFE_ID = Regex("[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
        private val ERROR_CODE = Regex("[A-Z][A-Z0-9_]{0,47}")

        fun validSummary(summary: WorkflowJobSummary?): Boolean =
            summary == null || (
                (summary.piElapsedMs == null || (summary.piElapsedMs.isFinite() && summary.piElapsedMs in 0.0..86_400_000.0)) &&
                    (summary.modelCalls == null || summary.modelCalls in 0..10_000) &&
                    (summary.fallbackRequests == null || summary.fallbackRequests in 0..1_000) &&
                    (summary.artifactId == null || (SAFE_ID.matches(summary.artifactId) && !summary.artifactId.startsWith("sk-", true))) &&
                    (summary.errorCode == null || ERROR_CODE.matches(summary.errorCode))
            )

        private fun merge(
            old: WorkflowJobSummary?,
            next: WorkflowJobSummary?,
        ): WorkflowJobSummary? =
            if (next == null) {
                old
            } else {
                WorkflowJobSummary(
                    next.piElapsedMs ?: old?.piElapsedMs,
                    next.modelCalls ?: old?.modelCalls,
                    next.fallbackRequests ?: old?.fallbackRequests,
                    next.artifactId ?: old?.artifactId,
                    next.errorCode ?: old?.errorCode,
                )
            }

        fun validRecording(value: RecordedWorkflow): Boolean =
            value.schemaVersion == 1 &&
                value.source == "android-accessibility-events" && value.targetPackage == "com.xingin.xhs" &&
                value.needsReview && !value.hasPairedVerification && SAFE_ID.matches(value.id) &&
                value.name.isNotBlank() && value.name.length <= 100 && value.events.size in 1..RecordingSession.MAX_EVENTS &&
                value.events.all { event ->
                    event.selector.packageName == value.targetPackage && event.type in setOf("click", "long_click", "text", "scroll") &&
                        event.evidence?.isBounded() != false &&
                        (
                            event.selector.semanticRole == null || event.evidence?.supportsCandidate(
                                event.selector.semanticRole,
                                event.selector.contextTitle,
                            ) == true
                        ) &&
                        listOf(
                            event.selector.className,
                            event.selector.resourceId,
                            event.selector.text,
                            event.selector.contentDescription,
                            event.value,
                        ).all {
                            it ==
                                null ||
                                it.length <= RecordingSession.MAX_TEXT
                        }
                }
    }
}
