package com.danielealbano.androidremotecontrolmcp.services.recording

import kotlinx.serialization.json.Json
import java.io.File

/** Private files only. Files must stay within the provided recording directory. */
class RecordingFileStore(
    private val directory: File,
) {
    private val json =
        Json {
            encodeDefaults = true
            prettyPrint = true
        }

    fun save(recording: RecordedWorkflow): File {
        require(SAFE_ID.matches(recording.id)) { "Invalid recording identifier" }
        val encoded = encode(recording)
        require(encoded.toByteArray(Charsets.UTF_8).size <= MAX_BYTES) { "Recording exceeds size limit" }
        check(directory.isDirectory || directory.mkdirs()) { "Cannot create recording directory" }
        val file = File(directory, "${recording.id}.json")
        val temporary = File(directory, "${recording.id}.tmp")
        try {
            temporary.writeText(encoded, Charsets.UTF_8)
            check(temporary.renameTo(file)) { "Cannot persist recording" }
        } finally {
            temporary.delete()
        }
        return file
    }

    fun latest(): RecordedWorkflow? =
        directory
            .listFiles()
            ?.asSequence()
            ?.filter { it.isFile && it.extension == "json" && it.length() <= MAX_BYTES }
            ?.sortedByDescending { it.lastModified() }
            ?.mapNotNull { file ->
                runCatching { json.decodeFromString<RecordedWorkflow>(file.readText(Charsets.UTF_8)) }
                    .getOrNull()
                    ?.takeIf { valid(it) && file.name == "${it.id}.json" }
            }?.firstOrNull()

    fun path(recording: RecordedWorkflow): File = File(directory, "${recording.id}.json")

    fun encode(recording: RecordedWorkflow): String = json.encodeToString(recording)

    private fun valid(recording: RecordedWorkflow): Boolean =
        SAFE_ID.matches(recording.id) && recording.schemaVersion == 1 &&
            recording.events.size <= RecordingSession.MAX_EVENTS &&
            recording.source == "android-accessibility-events" && recording.needsReview && !recording.hasPairedVerification

    companion object {
        private const val MAX_BYTES = 1_500_000L
        private val SAFE_ID = Regex("[A-Za-z0-9_-]{1,80}")
    }
}
