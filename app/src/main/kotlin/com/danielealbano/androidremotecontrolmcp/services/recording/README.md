# Android workflow recording draft

`WorkflowRecorder` is an application-scoped singleton. The Compose screen starts it before launching the selected application. `McpAccessibilityService` feeds it actionable accessibility events while connected. Capture is local and opt-in; there is no recording upload, video capture, or model request in this component.

## State and limits

`IDLE → RECORDING ⇄ PAUSED → REVIEW`; `discard()` clears the current in-memory draft without deleting existing files. Only the configured target package is recorded. The application's own UI and keyboard packages emit events under different package names and are excluded. Password events and password source nodes/ancestors are excluded before reading their text. Only click, long-click, text-change, and scroll events are accepted. Unlabeled controls with no resource ID are skipped rather than inventing a replay target.

Consecutive text events for the same selector are merged. Every text field is capped at 512 characters; a recording is limited to 200 events. Reaching the limit explicitly pauses capture. Accessibility service destruction pauses recording and presents an error; capture does not silently continue. Events do not survive process death until the user ends and saves a draft.

`stop()` creates Review immediately and saves asynchronously to `filesDir/workflow-recordings/{id}.json` using a temporary file followed by rename. Disk errors remain visible and do not prevent exporting the in-memory JSON through the system document picker. The most recent valid saved draft reopens in Review at application startup. Drafts are never resumed automatically. This MVP exposes the latest draft, not a multi-recording library.

## JSON schema version 1

```json
{
  "schemaVersion": 1,
  "source": "android-accessibility-events",
  "id": "a UUID",
  "name": "搜索并收藏帖子",
  "targetPackage": "com.xingin.xhs",
  "startedAtEpochMs": 0,
  "endedAtEpochMs": 1000,
  "needsReview": true,
  "hasPairedVerification": false,
  "events": [
    {
      "sequence": 1,
      "elapsedMs": 250,
      "type": "click",
      "selector": {
        "packageName": "com.xingin.xhs",
        "className": "android.widget.Button",
        "resourceId": "com.xingin.xhs:id/search",
        "text": "搜索",
        "contentDescription": null
      },
      "value": null,
      "scrollFromIndex": null,
      "scrollToIndex": null
    }
  ]
}
```

`type` is `click`, `long_click`, `text`, or `scroll`. A `text` event uses the current input as `value` and omits selector text to keep the locator independent of typed content. Scroll indices are observation evidence only; this schema does not invent a swipe direction or coordinate when Android does not provide one. No opaque accessibility node ID or absolute touch position is persisted.

## Evidence boundary and PI

Accessibility events cannot distinguish a human tap from an MCP-generated action. The source describes the capture mechanism, **not** the actor. The draft has no paired before/after snapshots or outcome verification. It must be reviewed and supplemented before compiling into a PI-executable skill. It must never be silently treated as an already validated workflow. In particular, a recorded click is not sufficient evidence that the intended target was reached or that a like became active.

The paired PI demonstration recorder is a separate computer-side component. Its before/after snapshots and declared actor/source must remain distinct from this raw Android draft. Existing Android MCP actions remain the execution transport; no unauthenticated recording endpoint or new remote recording authority is introduced here.

## Validation

`RecordingSessionTest` exercises state gating, package/password exclusion, paused capture, text coalescing, event limits, disconnect behavior, safe persistence, corrupt-file handling, review restoration, and the explicit unverified schema.
