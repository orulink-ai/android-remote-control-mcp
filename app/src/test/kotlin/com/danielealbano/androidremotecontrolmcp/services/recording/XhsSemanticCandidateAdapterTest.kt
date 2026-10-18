package com.danielealbano.androidremotecontrolmcp.services.recording

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

class XhsSemanticCandidateAdapterTest {
    private val like = RecordingNodeSnapshot(RecordedBounds(699, 2990, 956, 3144), listOf("1605"), true)
    private val favorite = RecordingNodeSnapshot(RecordedBounds(956, 2990, 1218, 3144), listOf("收藏177"), true)
    private val title = RecordingNodeSnapshot(RecordedBounds(20, 500, 1400, 700), listOf(XhsSemanticCandidateAdapter.TARGET_TITLE), false)
    private val author = RecordingNodeSnapshot(RecordedBounds(20, 200, 800, 300), listOf("作者小狮日记"), false)

    private fun snapshot(
        source: RecordingNodeSnapshot = like,
        nodes: List<RecordingNodeSnapshot> = listOf(like, favorite, title, author),
    ) = RecordingCaptureSnapshot(source, nodes, 1440, 3200)

    @Test
    fun `bare like click survives only as an anchored target-specific candidate`() {
        val candidate = XhsSemanticCandidateAdapter.identify(snapshot())!!
        assertEquals("like_toggle", candidate.role)
        assertEquals(XhsSemanticCandidateAdapter.TARGET_TITLE, candidate.contextTitle)
        assertEquals("xhs-toolbar-candidate", candidate.evidence.source)
        assertEquals(favorite.bounds, candidate.evidence.favoriteAnchorBounds)
        assertEquals(listOf("收藏177"), candidate.evidence.favoriteAnchorLabels)
        assertEquals(listOf("1605"), candidate.evidence.sourceLabels)
        assertEquals("小狮日记", candidate.evidence.contextAuthor)
    }

    @Test
    fun `favorite has the same captured target and explicit semantic anchor`() {
        val candidate = XhsSemanticCandidateAdapter.identify(snapshot(favorite))!!
        assertEquals("collect_toggle", candidate.role)
        assertEquals(favorite.bounds, candidate.evidence.sourceBounds)
    }

    @Test
    fun `counts without favorite or target author are not semantic evidence`() {
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(nodes = listOf(like, title, author))))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(nodes = listOf(like, favorite, title))))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(nodes = listOf(like, favorite, author))))
    }

    @Test
    fun `ambiguous anchor or like target fails closed`() {
        val otherAnchor = favorite.copy(bounds = RecordedBounds(960, 2990, 1220, 3144))
        val otherLike = like.copy(bounds = RecordedBounds(700, 2992, 955, 3142))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(nodes = listOf(like, favorite, otherAnchor, title, author))))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(nodes = listOf(like, otherLike, favorite, title, author))))
    }

    @Test
    fun `off row nonadjacent nonfooter and incomplete trees cannot classify likes`() {
        val wrongRow = like.copy(bounds = RecordedBounds(699, 2500, 956, 2654))
        val separated = like.copy(bounds = RecordedBounds(600, 2990, 850, 3144))
        val topFavorite = favorite.copy(bounds = RecordedBounds(956, 300, 1218, 454))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(wrongRow, listOf(wrongRow, favorite, title, author))))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(separated, listOf(separated, favorite, title, author))))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot(nodes = listOf(like, topFavorite, title, author))))
        assertNull(XhsSemanticCandidateAdapter.identify(snapshot().copy(complete = false)))
    }

    @Test
    fun `post source descendants retain the fixed title as an open candidate`() {
        val card = RecordingNodeSnapshot(RecordedBounds(0, 500, 700, 1800), listOf(XhsSemanticCandidateAdapter.TARGET_TITLE), true)
        val candidate = XhsSemanticCandidateAdapter.identify(snapshot(card, listOf(card)))!!
        assertEquals("open_target", candidate.role)
        assertEquals("source-descendants-v1", candidate.evidence.source)
        assertEquals(listOf(XhsSemanticCandidateAdapter.TARGET_TITLE), candidate.evidence.sourceLabels)
    }
}
