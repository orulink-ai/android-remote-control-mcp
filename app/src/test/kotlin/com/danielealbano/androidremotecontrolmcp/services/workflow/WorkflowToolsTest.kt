package com.danielealbano.androidremotecontrolmcp.services.workflow

import com.danielealbano.androidremotecontrolmcp.data.model.ToolPermissionsConfig
import com.danielealbano.androidremotecontrolmcp.mcp.McpToolException
import com.danielealbano.androidremotecontrolmcp.mcp.tools.LoggedToolRegistrar
import com.danielealbano.androidremotecontrolmcp.mcp.tools.WorkflowTools
import com.danielealbano.androidremotecontrolmcp.mcp.tools.registerWorkflowTools
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import io.mockk.verify
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class WorkflowToolsTest {
    private val coordinator = mockk<WorkflowJobCoordinator>(relaxed = true)
    private val tools = WorkflowTools(coordinator)

    @Test
    fun `three tools register through logged registrar using configured prefix and disabled tools do not register`() {
        val registrar = mockk<LoggedToolRegistrar>(relaxed = true)
        registerWorkflowTools(registrar, coordinator, "android_", ToolPermissionsConfig(disabledTools = setOf("workflow_update")))
        verify(exactly = 1) { registrar.addTool("workflow_claim", "android_workflow_claim", any(), any(), any()) }
        verify(exactly = 1) { registrar.addTool("workflow_status", "android_workflow_status", any(), any(), any()) }
        verify(exactly = 0) { registrar.addTool("workflow_update", any(), any(), any(), any()) }
    }

    @Test
    fun `claim polls cannot carry recording and result uses JSON data block`() =
        runTest {
            coEvery { coordinator.claim("worker-a") } returns WorkflowClaim()
            val result = tools.claim(buildJsonObject { put("worker_id", "worker-a") })
            val payload = Json.parseToJsonElement((result.content.last() as TextContent).text).jsonObject
            assertTrue(payload.containsKey("job"))
            coVerify(exactly = 1) { coordinator.claim("worker-a") }
            val invalid =
                buildJsonObject {
                    put("worker_id", "worker-a")
                    put("recording", "private")
                }
            var rejected = false
            try {
                tools.claim(invalid)
            } catch (_: McpToolException.InvalidParams) {
                rejected = true
            }
            assertTrue(rejected)
            coVerify(exactly = 1) { coordinator.claim(any()) }
        }

    @Test
    fun `status response does not expose recording owner or claim token`() =
        runTest {
            coEvery { coordinator.heartbeat("worker-a", null, null) } returns
                WorkflowJobUiState(
                    latestJob = WorkflowJob("job-1", "recording-1", "测试", WorkflowJobStatus.RUNNING, 1, 2),
                    companionOnline = true,
                )
            val result = tools.status(buildJsonObject { put("worker_id", "worker-a") })
            val payload = (result.content.last() as TextContent).text
            assertFalse(payload.contains("claimToken"))
            assertFalse(payload.contains("claimedBy"))
            assertFalse(payload.contains("\"recording\":"))
            assertTrue(payload.contains("\"leaseSeconds\":90"))
        }

    @Test
    fun `update accepts bounded structured summary and rejects free form secret fields`() =
        runTest {
            coEvery { coordinator.update("job-1", "opaque", WorkflowJobStatus.RUNNING, any()) } returns
                WorkflowJob("job-1", "recording-1", "测试", WorkflowJobStatus.RUNNING, 1, 2)
            tools.update(
                buildJsonObject {
                    put("job_id", "job-1")
                    put("claim_token", "opaque")
                    put("status", "running")
                    putJsonObject("summary") {
                        put("modelCalls", 1)
                        put("artifactId", "workflow-1")
                    }
                },
            )
            coVerify(exactly = 1) {
                coordinator.update(
                    "job-1",
                    "opaque",
                    WorkflowJobStatus.RUNNING,
                    WorkflowJobSummary(modelCalls = 1, artifactId = "workflow-1"),
                )
            }
            var rejected = false
            try {
                tools.update(
                    buildJsonObject {
                        put("job_id", "job-1")
                        put("claim_token", "opaque")
                        put("status", "running")
                        putJsonObject("summary") {
                            put("privatePath", "/private/file")
                            put("token", "private")
                        }
                    },
                )
            } catch (_: McpToolException.InvalidParams) {
                rejected = true
            }
            assertTrue(rejected)
            coVerify(exactly = 1) { coordinator.update(any(), any(), any(), any()) }
        }

    @Test
    fun `missing worker non-string worker invalid status and malformed heartbeat are rejected`() =
        runTest {
            var failures = 0
            for (params in listOf(null, buildJsonObject { put("worker_id", 123) })) {
                try {
                    tools.claim(params)
                } catch (_: McpToolException.InvalidParams) {
                    failures++
                }
            }
            try {
                tools.update(
                    buildJsonObject {
                        put("job_id", "job-1")
                        put("claim_token", "opaque")
                        put("status", "queued")
                    },
                )
            } catch (
                _: McpToolException.InvalidParams,
            ) {
                failures++
            }
            try {
                tools.status(
                    buildJsonObject {
                        put("worker_id", "worker-a")
                        put("job_id", "job-1")
                    },
                )
            } catch (
                _: McpToolException.InvalidParams,
            ) {
                failures++
            }
            assertTrue(failures == 4)
            coVerify(exactly = 0) { coordinator.claim(any()) }
            coVerify(exactly = 0) { coordinator.update(any(), any(), any(), any()) }
            coVerify(exactly = 0) { coordinator.heartbeat(any(), any(), any()) }
        }
}
