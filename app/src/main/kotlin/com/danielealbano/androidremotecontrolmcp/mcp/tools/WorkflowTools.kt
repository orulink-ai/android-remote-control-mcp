package com.danielealbano.androidremotecontrolmcp.mcp.tools

import com.danielealbano.androidremotecontrolmcp.data.model.ToolPermissionsConfig
import com.danielealbano.androidremotecontrolmcp.mcp.McpToolException
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobCoordinator
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobException
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobQueue
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobStatus
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobSummary
import io.modelcontextprotocol.kotlin.sdk.types.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/** Authenticated companion coordination only; no remote tool can submit or alter a recording. */
class WorkflowTools(
    private val coordinator: WorkflowJobCoordinator,
) {
    private val json = Json { encodeDefaults = true }

    suspend fun claim(arguments: JsonObject?): CallToolResult =
        result {
            requireKeys(arguments, setOf("worker_id"))
            json.encodeToJsonElement(coordinator.claim(requiredString(arguments, "worker_id")))
        }

    suspend fun update(arguments: JsonObject?): CallToolResult =
        result {
            requireKeys(arguments, setOf("job_id", "claim_token", "status", "summary"))
            val status = requiredString(arguments, "status")
            val parsedStatus =
                WorkflowJobStatus.entries.firstOrNull { it.name.lowercase() == status && it in UPDATE_STATUSES }
                    ?: throw McpToolException.InvalidParams("Unsupported workflow status")
            val summary =
                arguments?.get("summary")?.takeIf { it != JsonNull }?.let { element ->
                    try {
                        json.decodeFromJsonElement<WorkflowJobSummary>(element)
                    } catch (
                        _: IllegalArgumentException,
                    ) {
                        throw McpToolException.InvalidParams("Invalid workflow summary")
                    }
                }
            if (!WorkflowJobQueue.validSummary(summary)) throw McpToolException.InvalidParams("Invalid workflow summary")
            json.encodeToJsonElement(
                coordinator.update(requiredString(arguments, "job_id"), requiredString(arguments, "claim_token"), parsedStatus, summary),
            )
        }

    suspend fun status(arguments: JsonObject?): CallToolResult =
        result {
            requireKeys(arguments, setOf("worker_id", "job_id", "claim_token"))
            val worker = requiredString(arguments, "worker_id")
            val jobId = optionalString(arguments, "job_id")
            val token = optionalString(arguments, "claim_token")
            if ((jobId == null) != (token == null)) throw McpToolException.InvalidParams("Heartbeat requires both job and claim token")
            val state = coordinator.heartbeat(worker, jobId, token)
            buildJsonObject {
                put("companionOnline", state.companionOnline)
                put("job", state.latestJob?.let { json.encodeToJsonElement(it) } ?: JsonNull)
                put("heartbeatSeconds", WorkflowJobQueue.HEARTBEAT_SECONDS)
                put("leaseSeconds", WorkflowJobQueue.LEASE_SECONDS)
            }
        }

    fun register(
        registrar: LoggedToolRegistrar,
        prefix: String,
        permissions: ToolPermissionsConfig,
    ) {
        val handlers: Map<String, suspend (JsonObject?) -> CallToolResult> =
            mapOf(
                "workflow_claim" to ::claim,
                "workflow_update" to ::update,
                "workflow_status" to ::status,
            )
        for ((name, handler) in handlers) {
            if (!permissions.isToolEnabled(name)) continue
            registrar.addTool(name, "$prefix$name", description(name), schema(name)) { request -> handler(request.arguments) }
        }
    }

    private suspend fun result(block: suspend () -> JsonElement): CallToolResult =
        try {
            // Keep the last content block machine-readable; the warning remains a separate block.
            McpToolUtils.untrustedResult(listOf(TextContent(text = block().toString())))
        } catch (error: WorkflowJobException) {
            when (error.code) {
                "STORAGE_FAILURE" -> throw McpToolException.ActionFailed("Workflow storage unavailable")
                "INVALID_CLAIM" -> throw McpToolException.PermissionDenied("Workflow claim is not valid")
                else -> throw McpToolException.InvalidParams("Workflow request rejected: ${error.code}")
            }
        }

    private fun requiredString(
        arguments: JsonObject?,
        name: String,
    ): String =
        optionalString(arguments, name)
            ?: throw McpToolException.InvalidParams("Missing workflow parameter: $name")

    private fun optionalString(
        arguments: JsonObject?,
        name: String,
    ): String? {
        val element = arguments?.get(name) ?: return null
        if (element == JsonNull) return null
        val value = element as? JsonPrimitive ?: throw McpToolException.InvalidParams("Invalid workflow parameter: $name")
        if (!value.isString || value.content.isBlank() ||
            value.content.length > MAX_PARAMETER_LENGTH
        ) {
            throw McpToolException.InvalidParams("Invalid workflow parameter: $name")
        }
        return value.content
    }

    private fun requireKeys(
        arguments: JsonObject?,
        allowed: Set<String>,
    ) {
        if (arguments?.keys?.any { it !in allowed } == true) throw McpToolException.InvalidParams("Unexpected workflow parameter")
    }

    private fun description(name: String): String =
        when (name) {
            "workflow_claim" -> {
                "Claim only a phone workflow explicitly queued by the device user. " +
                    "Repeating the owning worker claim is idempotent."
            }

            "workflow_update" -> {
                "Report a claimed workflow's ordered state and bounded non-sensitive result summary. " +
                    "Requires the matching claim token."
            }

            else -> {
                "Heartbeat companion presence and optionally renew the matching active claim lease. " +
                    "Returns public status without recording or token."
            }
        }

    private fun schema(name: String): ToolSchema {
        val update = name == "workflow_update"
        val properties =
            buildJsonObject {
                if (!update) {
                    putJsonObject("worker_id") {
                        put("type", "string")
                        put("maxLength", 64)
                    }
                }
                if (name != "workflow_claim") {
                    putJsonObject("job_id") {
                        put("type", "string")
                        put("maxLength", 96)
                    }
                    putJsonObject("claim_token") {
                        put("type", "string")
                        put("maxLength", MAX_PARAMETER_LENGTH)
                    }
                }
                if (update) {
                    putJsonObject("status") { put("type", "string") }
                    putJsonObject("summary") {
                        put("type", "object")
                        put("additionalProperties", false)
                        putJsonObject("properties") {
                            putJsonObject("piElapsedMs") { put("type", "number") }
                            putJsonObject("modelCalls") { put("type", "integer") }
                            putJsonObject("fallbackRequests") { put("type", "integer") }
                            putJsonObject("artifactId") { put("type", "string") }
                            putJsonObject("errorCode") { put("type", "string") }
                        }
                    }
                }
            }
        return ToolSchema(
            properties = properties,
            required = if (update) listOf("job_id", "claim_token", "status") else listOf("worker_id"),
        )
    }

    companion object {
        private const val MAX_PARAMETER_LENGTH = 128
        private val UPDATE_STATUSES =
            setOf(
                WorkflowJobStatus.VALIDATING,
                WorkflowJobStatus.GENERATING,
                WorkflowJobStatus.RUNNING,
                WorkflowJobStatus.SUCCEEDED,
                WorkflowJobStatus.FAILED,
            )
    }
}

fun registerWorkflowTools(
    registrar: LoggedToolRegistrar,
    coordinator: WorkflowJobCoordinator,
    toolNamePrefix: String,
    perms: ToolPermissionsConfig,
) {
    WorkflowTools(coordinator).register(registrar, toolNamePrefix, perms)
}
