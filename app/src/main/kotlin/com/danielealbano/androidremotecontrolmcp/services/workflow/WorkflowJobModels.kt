package com.danielealbano.androidremotecontrolmcp.services.workflow

import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflow
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class WorkflowJobStatus {
    @SerialName("queued")
    QUEUED,

    @SerialName("validating")
    VALIDATING,

    @SerialName("generating")
    GENERATING,

    @SerialName("running")
    RUNNING,

    @SerialName("succeeded")
    SUCCEEDED,

    @SerialName("failed")
    FAILED,

    @SerialName("cancelled")
    CANCELLED,
    ;

    val isActive: Boolean get() = this in setOf(QUEUED, VALIDATING, GENERATING, RUNNING)
}

enum class WorkflowJobIssue { INVALID_RECORDING, ACTIVE_JOB, COMPANION_OFFLINE, STORAGE_FAILURE }

@Serializable
data class WorkflowJobSummary(
    val piElapsedMs: Double? = null,
    val modelCalls: Int? = null,
    val fallbackRequests: Int? = null,
    val artifactId: String? = null,
    val errorCode: String? = null,
)

@Serializable
data class WorkflowJob(
    val id: String,
    val recordingId: String,
    val name: String,
    val status: WorkflowJobStatus,
    val createdAtEpochMs: Long,
    val updatedAtEpochMs: Long,
    val summary: WorkflowJobSummary? = null,
)

data class WorkflowJobUiState(
    val latestJob: WorkflowJob? = null,
    val companionOnline: Boolean = false,
    val error: WorkflowJobIssue? = null,
)

@Serializable
data class WorkflowTask(
    val query: String = "小狮日记",
    val expectedFirstTitle: String = "当我闯进一个限时48小时的创造营",
)

@Serializable
data class WorkflowClaimJob(
    val id: String,
    val recordingId: String,
    val name: String,
    val recording: RecordedWorkflow,
    val task: WorkflowTask = WorkflowTask(),
    val operation: String = "generate_and_run",
)

@Serializable
data class WorkflowClaim(
    val job: WorkflowClaimJob? = null,
    val claimToken: String? = null,
)

/** Private persistence only: never serialize this into status/update responses. */
@Serializable
data class StoredWorkflowJob(
    val job: WorkflowJob,
    val recording: RecordedWorkflow,
    val claimedBy: String? = null,
    val claimToken: String? = null,
    val leaseExpiresAtMs: Long? = null,
    val schemaVersion: Int = 1,
)

class WorkflowJobException(
    val code: String,
) : RuntimeException("Workflow request rejected: $code")
