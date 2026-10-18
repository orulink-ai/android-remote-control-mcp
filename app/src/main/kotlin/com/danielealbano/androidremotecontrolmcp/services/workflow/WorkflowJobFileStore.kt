package com.danielealbano.androidremotecontrolmcp.services.workflow

import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream

interface WorkflowJobStore {
    fun load(): StoredWorkflowJob?

    fun save(job: StoredWorkflowJob)
}

/** A single private durable slot; UI must explicitly submit before any recording is shared. */
class WorkflowJobFileStore(
    private val directory: File,
) : WorkflowJobStore {
    private val json = Json { encodeDefaults = true }

    override fun load(): StoredWorkflowJob? {
        val file = File(directory, FILE_NAME)
        if (!file.exists()) return null
        check(file.isFile && file.length() <= MAX_BYTES) { "Invalid workflow store" }
        return json.decodeFromString(file.readText(Charsets.UTF_8))
    }

    override fun save(job: StoredWorkflowJob) {
        val bytes = json.encodeToString(job).toByteArray(Charsets.UTF_8)
        check(bytes.size <= MAX_BYTES) { "Workflow store size limit" }
        check(directory.isDirectory || directory.mkdirs()) { "Workflow store unavailable" }
        val temporary = File(directory, "latest.tmp")
        try {
            FileOutputStream(temporary).use { stream ->
                stream.write(bytes)
                stream.fd.sync()
            }
            check(temporary.renameTo(File(directory, FILE_NAME))) { "Workflow store unavailable" }
        } finally {
            temporary.delete()
        }
    }

    companion object {
        private const val FILE_NAME = "latest.json"
        private const val MAX_BYTES = 1_750_000
    }
}
