# Moirai architecture

Moirai is intentionally a Python application. Its workload is dominated by LLM orchestration, validated JSON, text processing and local I/O rather than CPU-bound computation. FastAPI, Pydantic and the OpenAI-compatible SDK fit those boundaries well. A rewrite in another language would add migration risk without addressing the original design problems.

The product name is **Moirai**. Persisted folders such as `.novelengine_transactions`,
`.novelengine_trash` and `.novelengine_locks`, plus logger namespaces, retain their historical
identifiers as a compatibility boundary; renaming them would strand recovery journals or stale
revision tombstones in existing libraries.

## Module boundaries

```text
main.py / web_ui.py
        │
        ├── api/schemas.py              HTTP request contracts
        ├── api/downloads.py            disposable HTTP download lifetime
        ├── services/generation_jobs.py long-running local jobs
        │
        ▼
pipeline/novel_pipeline.py              workflow orchestration
        │
        ├── agents/                     LLM roles and prompts
        └── core/                       domain state and infrastructure
             ├── schemas.py             persisted domain contracts
             ├── planning.py            initial package/outline publication contracts
             ├── state_manager.py       project aggregate and exports
             ├── storage.py             atomic files and project locks
             ├── project_transactions.py durable undo journals and crash recovery
             ├── env_file.py            atomic dotenv batch updates
             ├── project_ids.py         shared portable identifier rules
             ├── project_export.py      Markdown/TXT/EPUB publication
             ├── task_status.py         one local worker reservation
             ├── project_archive.py     validated backup import
             ├── project_trash.py       validated trash listing and restore
             ├── runtime_config.py      transactional live configuration
             ├── semantic_memory.py     deterministic Chinese recall
             └── knowledge_graph.py     entity and relation model

web/index.html                           browser UI
web/assets/                              checked-in offline CSS/JS/fonts
novel_projects/<id>/                     user-owned project data
```

`web_ui.py` is now the HTTP composition layer. It no longer embeds the full HTML/JavaScript application or owns the generation workflow. Read-only operations instantiate `NovelPipeline` cheaply because agents and the LLM client are lazy-loaded. Project lists and workspace details use chapter metadata only; the reader fetches one chapter body on demand.

## Download snapshots and publication files

Web Markdown/TXT/EPUB downloads load a complete prose snapshot under the project
locks, without hydrating semantic memory or generated side products. Corrupt
loaded data fails closed rather than producing an incomplete publication. The
locks are then released and the detached snapshot is rendered into a unique
temporary directory; a later edit cannot change the in-flight download. The
response carries the exported `X-Project-Revision`. Downloads never call the
editorial mutation helpers, rewrite project files or advance the revision.

ZIP backup downloads copy all project files under both locks and retain archive
path/link checks. All four formats share `TemporaryDownloadResponse`, whose
`finally` cleanup covers successful responses, HEAD/range errors and client
disconnect/cancellation. Deferred server path-send is disabled so the file cannot
be deleted before that server reads it. Files are streamed, not buffered in a
browser fetch, and each request has its own output path. A process kill can still
leave an OS temporary directory; this is not a crash-proof download cache.

CLI export and automatic chapter publication retain their existing transactional
in-project file destinations. They are intentionally separate from HTTP downloads.
Browser links use same-origin native attachment downloads and do not open tabs.

## Generation flow

1. The planner creates the concept, world and initial cast.
2. The architect creates the global outline and pacing schedule.
3. When generation reaches an unplanned frontier, the architect adds a validated rolling block of at most ten chapters. The block is capped by its declared volume range and the next existing outline, so it cannot cross a volume or overwrite planned material. The writer never silently generates a context-free chapter.
4. The director plans chapter beats; a deterministic local plan is used if that auxiliary call fails.
5. The writer expands each beat. The critic can request bounded rewrites.
6. Changed polishing output is audited again and accepted only on a genuine pass. Empty, failed or unaudited polish falls back to the selected draft. State extraction reads precisely the accepted final text.
7. The final chapter, semantic memory, graph and exports are persisted with a versioned state event bound to the exact accepted prose SHA-256.

Batch generation stops at the first failed chapter and surfaces the partial result. It never skips a failed number and continues writing a disconnected timeline. A state-extraction failure keeps the usable prose but marks that chapter as requiring reconciliation.

## Initial planning and command intent

Before the first planner call, `pending_initial_planning` commits the bounded
original idea, genre, commercial target and outline dimensions. Concept, world and
cast calls then publish their validated outputs one stage at a time. Each retry
receives detached copies of completed checkpoints and calls only the first missing
stage onward. Duplicate/blank character identities are rejected; each character's
`current_state` is an independent copy of `initial_state`, not a separate default
returned by a model. A complete cast checkpoint is promoted into canonical
concept/world/characters plus strict `pending_outline_plan` in one transaction.
Checkpoint values are read-only in the Web workspace and the raw original idea is
not returned by project detail/status APIs. Full backups intentionally retain it
so the workflow remains recoverable. Existing user projects are not migrated or
silently rewritten by this change.

The outline model call runs outside the short snapshot lock while the workflow
still owns the operation lease. Failed calls or invalid model output leave the
complete saved package resumable. Validated outline publication and clearing the
pending marker commit atomically. Validation follows the actual rolling-outline
contract: every requested macro volume must exist in order; the first volume must
provide a continuous prefix of at least the requested size; later volumes may
remain macro-only. Provided chapter IDs must be globally ordered and unique,
volume/pacing identities must agree, and pacing ranges must be coherent.
Every newly generated volume also declares an inclusive global `chapter_range`;
ranges start at chapter 1 and join without overlap or gaps. This is the durable
ownership boundary used for future planning, not an assumption that each volume
has the same length.

Rolling extension first derives one unplanned window from those ranges, clips it
before an existing future outline, and builds detached inputs for the model. It
passes the current volume, adjacent-volume macro goals, whole-book ending and any
future anchor. Returned chapters and pacing beats must be ordered one-for-one,
complete, nonblank and owned by the selected volume; invalid output leaves both
memory and disk unchanged. A detached candidate outline is validated before one
transaction publishes `global_outline.json` and aggregate state. Legacy outlines
with chapter anchors retain their old open-ended final-volume behavior; an empty
later legacy volume is ambiguous and fails with guidance rather than being guessed
or silently rewritten. A declared book end is a hard stop.

`resume_initialization` completes missing concept/world/cast stages;
`resume_initial_planning` completes only the outline. `resume-plan` chooses the
correct checkpoint automatically. CLI/Web continuation also resumes it before
writing new chapters.
Completed projects and mismatched retry parameters fail before model calls.
Legacy partial projects without recorded dimensions require both original
dimensions explicitly; missing foundational data is never guessed or replaced.
Both pending markers are part of the validated project/backup schema. Invalid
stage order, mixed checkpoint/canonical state, changed dimensions, oversized
request fields and corrupt imported checkpoints fail closed.

CLI `auto` and `plan` require a new identity; `continue-gen` owns continuation;
`chapter --rewrite` explicitly authorizes replacing an existing chapter. CLI
argument parsing, pipeline range calculation and worker preflight reject invalid
counts before planning. `BatchGenerationError` exposes the failure chapter and
completed prefix. CLI boundaries use nonzero exit codes and disable traceback
locals; unexpected failures and the terminal batch message do not dump provider
exception details.

`core/error_reporting.py` is the single privacy boundary for unexpected failures.
It assigns a 12-hex correlation ID to the exception object, so LLM transport,
pipeline, worker, CLI and HTTP layers reuse one identity instead of logging the
same failure repeatedly. Error records contain only the exception type chain,
source basenames/line numbers and hashed string context; exception messages,
notes, traceback locals, provider bodies, raw model output and arbitrary archive
member names are excluded. User-facing text is exposed only by explicitly marked,
application-authored exception classes. Optional audio, audit, polish, state,
short-drama and commercial stages follow the same rule; persisted failure reports
contain a correlation ID, not `str(exception)`. Diagnostic IDs aid correlation,
but are not a substitute for retaining backups or reproducing the failure.

## Chapter-time context and text/audit identity

`StateManager.state_before_chapter(N)` returns a detached state/graph through chapter
N-1. Historical requests replay the baseline and a complete, numerically ordered
event sequence; legacy requests need the exact prior v2/v3 snapshot and its graph.
Missing, corrupt or mismatched evidence fails before any planner/writer call. The
live aggregate is never temporarily rewound. Current editorial outlines and world
rules remain in use, but they are explicitly described as plans, not past events.

Character role/background/personality overrides apply to characters known in the
historical view. New dynamic edits are applied exactly at their recorded chapter
boundary, between story events. Checkpoints already contain all earlier events;
reapplying earlier edits over a checkpoint would incorrectly undo later plot
development. Rollback retires future edits as retained, inactive history. Only
actually changed fields become overrides, and identical forms leave files and the
revision untouched. Legacy undated dynamic values remain current-only unless a
dated replacement has taken ownership of that field. `core/character_overrides.py`
validates the bounded, versioned records independently of storage; both reads and
writes enforce the 2 MiB limit. Long-term recall always
excludes the requested chapter and all later chapters, even for chapters 1/2.
Writer and critic receive the same pre-chapter facts, relations and memories.

An earlier reconciliation marker blocks downstream automatic writing until that
chapter is repaired. If prior state is trustworthy, the UI can extract state from
the already accepted prose without invoking the writer or replacing any text,
audit history or human-review history. Pending chapters are processed strictly in
ascending order; the worker rechecks the captured project revision before the model
call. The prose, version-2 event, aggregate state, graph, snapshot and cleared marker
publish in one transaction. A failed extraction or stale/out-of-order request
publishes none of them. Prose and an extraction-failure marker likewise commit in
the same transaction during generation. Manual/local paragraph editing does not
require a historical graph.

For a legacy project with no trustworthy chapter-zero evidence, repair is never
inferred. The author explicitly selects any known characters whose stored
`initial_state` is genuinely pre-chapter-one, supplies a confirmation note, and
creates a baseline at chapter 0. Unselected characters, relations and hooks remain
absent. The previous active timeline is copied to a bounded retired evidence folder
with per-file SHA-256/size manifest before removal, and every existing chapter is
then marked for sequential state-only extraction.

Every new audit is locally stamped with the SHA-256 of the exact submitted text,
stage, attempt and service-error status. Draft selection prioritizes actual passes
over scores, then keeps the best reviewed failure if no draft passes. A critic
outage cannot discard an earlier reviewed draft. Strictness adjustment cannot
promote a rejected report that contains logic/OOC issues or revision directives.

`audit_history` remains chronological. `final_audit` is derived from a matching text
digest, not from the last attempt. Full chapter loads and saves recalculate this
binding; metadata-only read-only views carry its small stored summary. The reader
distinguishes passed, failed, unavailable and unverified. Manual edits preserve
the history but invalidate unmatched reports. Legacy reports without a digest are
unverified, not retroactively claimed to have audited the current text. A digest
establishes text identity, not literary quality or protection from deliberate
external file tampering. Changed polish incurs one additional critic call.

`human_review_history` records an author decision, note, UTC timestamp and the
SHA-256 of the exact reviewed prose. `current_human_review` is derived only from a
matching digest, so any prose edit invalidates the current approval without deleting
evidence. Chapter replacement merges prior machine and human histories instead of
discarding them. Human-review writes modify only the chapter record and metadata
under the same revision/transaction protocol; a no-memory view preserves the stored
memory count and cannot rewrite semantic shards. The UI renders only the newest 100
records of each type while the project backup retains the full stored history.

## Derived-projection validity

Short-drama scripts, reader simulations, narrative branches, the audio cast and
the commercial delivery package are rebuildable projections, not canonical story
facts. `StateManager` owns their validity boundary so every API, CLI and background
workflow gets the same behavior. Invalidation runs inside the project mutation that
publishes the changed source and removes both the loaded model and its persisted
artifact through the transaction journal. A downstream model failure therefore
cannot leave a previous projection visible as if it described the new source.

The dependency contract follows the actual model inputs:

- short drama and reader simulation: concept plus the selected chapter's title,
  text and word count;
- narrative branch: concept, world contract, the selected chapter outline and the
  character fields supplied to the brancher; chapter/state changes invalidate the
  affected and later branch points conservatively;
- audio cast: concept title/genre plus character name, role, personality and
  speaking style;
- commercial delivery: concept, world, protagonist, chapter source and total word
  count.

Concept changes invalidate all five projection families. World changes invalidate
branches and commercial delivery. Character changes always invalidate branches and
commercial delivery, and invalidate audio only when an audio input changed. Outline
changes invalidate only the changed chapter's branch result. Prose changes invalidate
that chapter's short drama and reader report plus branch results at or after it.
Identical source publication is intentionally a no-op. APIs edit detached concept and
outline copies; `set_global_outline` also compares a legacy in-place caller against the
last committed file so mutation aliasing cannot conceal a change. Rollback drops every
branch result because it restores the character timeline, while chapter-bound projections
beyond the rollback point are pruned.

Invalidation is paired with an explicit recovery path. The Web API can rebuild one
selected chapter's short-drama script, reader report or branch simulation, rebuild the
whole audio cast, or rebuild the commercial package without touching prose. Each request
carries the loaded project revision, reserves the single background slot, and rechecks
that revision under the project operation lease before constructing or calling the
relevant model component. Reader and branch simulation use the same queued contract rather
than occupying an HTTP request thread while a model runs.
A queued request made stale by a later edit therefore terminates without a provider call.
The browser keeps the initiating project/chapter identity, refreshes only that project on
completion and never falls back to another chapter's script when the selected chapter has
no current projection.

## Persistence invariants

- Project IDs are strict, cross-platform-safe identifiers. Path aliases, traversal and Windows device names are rejected.
- JSON and text files use flushed temporary files followed by `os.replace`, so readers never see partially written individual files. During a project mutation, temporary files and first-write undo copies live in a private transaction directory on the same filesystem.
- Multi-file StateManager mutations use a durable undo journal. Undo records and original bytes are flushed before each write/delete/move. A commit marker decides whether restart keeps the new revision or restores the old file set. Recovery validates every backup checksum and path before touching files, checkpoints each undo operation, and refuses unsafe/corrupt recovery while preserving the journal.
- Re-entrant, OS-backed operation locks cover the complete Web/CLI generation workflow, including model calls and gaps between chapters. Acquisition is bounded and competing writers fail before invoking a model. A separate short snapshot lock protects consistent reads during disk writes; readers remain available during model calls.
- External revision tombstones plus aggregate-file fingerprints reject stale StateManager instances, including after deletion, import or rename. Lightweight or damaged project views cannot write. HTTP reports busy as 423 and stale/incomplete state as 409; CLI reports busy without a traceback.
- `project_state.json` contains only the aggregate state. Prose stays in independent chapter files, chapter navigation uses small metadata shards, and semantic memory is stored per chapter. Appending chapters therefore no longer rewrites all prior prose or all prior memory.
- Legacy aggregate projects remain readable and migrate to the sharded layout on their next successful mutation.
- The Web app reserves its single local generation slot before queuing a background task. Two simultaneous clicks cannot queue duplicate writers.
- Background workers must present the exact reservation token; a delayed or duplicated callback cannot take over a newer task's status.
- Every newly generated or reconciled chapter stores a `state_events/chapter_NNN.json` event. Version 2 binds the event to the exact chapter prose SHA-256; a mismatched body/event pair is rejected by a full load. Rewriting an event-sourced historical chapter replays all following events from `state_events/baseline.json`; removed items, relations and hooks do not leak from the old draft.
- Explicit character-card edits live in versioned `manual_overrides.json`. Static editorial fields apply to every known appearance; dynamic level/location/health/mood edits record `effective_after_chapter` and are interleaved with chapter events during replay. They cannot leak into earlier chapters or override later story events, and rollback excludes future-dated edits. Legacy undated dynamic records remain current-only. Corrupt override or reconciliation files make the loaded project read-only.
- Direct manual prose edits are marked in `reconciliation_required.json` until the affected chapter is regenerated or its existing prose is processed by the state-only reconciliation action.
- Rollback truncates chapters, snapshots, short-drama files, reader simulations and release files together, and clears all branch simulations whose current-character inputs were restored.
- Rollback rewrites retained chapter JSON/TXT and metadata from the snapshot, so restarting cannot resurrect edited prose. Timeline filenames are sorted by chapter number rather than lexicographically, including after chapter 999.

## Browser edit preconditions and request ownership

Read responses expose a project-bound, opaque SHA-256 `revision` built from the
loaded aggregate version and external revision tombstone. A chapter body and its
revision are read under the same snapshot lock. Returning a fresh disk token with
stale in-memory content is prohibited.

Destructive/editorial JSON requests require `expected_revision`; delete requires
the revision in `If-Match` (the raw 64-character hexadecimal token). Missing
preconditions return 428 and mismatches return 409 without invoking a model or
mutating project files. StateManager's locked stale-view check still guards the
gap between HTTP validation and mutation. Web continuation, queued regeneration and every
model-backed projection task check the captured revision again inside the worker's project
operation lock. A stale continuation therefore stops before planning recovery, outline or
chapter model calls; direct CLI continuation remains protected by the operation lease.

Browser editors capture their project, chapter and loaded revision when opened.
Project/chapter/library request generations discard late successes and failures;
chapter metadata cannot be paired with a different body revision. Save responses
cannot close a newer editor or navigate away from another selected book. Conflict
responses keep unsaved input visible. Task polling has one in-flight request, tracks the
task ID returned at admission so even a job that finishes before the first status response
still refreshes, and only refreshes the current project if the task belongs to it and
selection has not changed since the poll began. These preconditions cover application-mediated
changes, not arbitrary hostile modifications made directly on disk.

In-place project refresh is distinct from explicit project selection. Refresh
captures the current tab, chapter number (not merely its array index), expanded
directory and scroll positions. Lazy chapter text restores its saved reader
offset only after rendering and only while its request generation is still
current. A deleted chapter falls back to a nearby index at offset zero. Direct
book selection resets the view. Task completion uses the same preservation path.
Tabs use a roving tab stop with ArrowLeft/ArrowRight/Home/End support, paired
tab/tabpanel roles and explicit selection state. Native book/chapter buttons,
associated form labels and contextual edit names support keyboard navigation;
decorative font icons do not contribute glyphs to action names. These changes
are not a claim of complete screen-reader or WCAG conformance.

## Transaction recovery and local trash

Journals live at `<OUTPUT_DIR>/.novelengine_transactions/<transaction-id>/`, outside
project directories so a rename or delete cannot strand the recovery record. They
are bounded to 2 GiB of previous file contents, 50,000 undo records and a 32 MiB log.
Each changed file is backed up once per transaction, not once per nested save.
Normal commits remove their private journal. A committed cleanup failure can be
retried without undoing committed data.

Project loads, existence checks, lists, import publication and background jobs
recover abandoned journals under all participant operation/snapshot locks. They
never undo a live writer. Rename protects both names; Windows case aliases map to
the same protected project and original filenames are retained in undo records.

The bookshelf also exposes path-free structured recovery diagnostics. A corrupt
journal whose participants are trustworthy blocks only those projects; an unreadable
or missing header has unknown ownership and blocks all project publication. New,
import, trash-restore and ordinary writes cannot bypass that fail-closed boundary.
The diagnostic view preserves the journal, reports a stable ID and gives backup-first
operator guidance. Non-regular roots/logs/markers and transient recovery I/O errors
have explicit fault-injection coverage rather than falling through as HTTP 500.

Delete is a recoverable directory move to
`<OUTPUT_DIR>/.novelengine_trash/<original-id>-<transaction-id>/`. A pre-commit crash
undoes that move; after commit the files stay in trash. The Web trash view lists
validated entries and restores under the `novel_id` stored in `project_state.json`.
Restore reserves the local mutation slot, validates the trash directory and project
identity again, acquires project locks, and refuses to overwrite an existing target.
There is no automatic trash purge.

Fault tests cover exceptions after every chapter file publication, abrupt process
exit before/after commit, interrupted rename/delete, interruption during undo, and
lost commit acknowledgement. These tests establish process-crash recovery on the
tested Windows filesystem, not a guarantee against damaged hardware, external
file edits, network-filesystem semantics or physical power loss. Files are fsynced;
directory fsync is also used on POSIX, whose runtime matrix remains unexecuted.

## Snapshot compatibility and rollback safety

- v1: legacy state-only snapshot; eligible only as an explicitly labelled approximate rollback point when its project and progress agree and it contains no future chapter.
- v2: full state, graph and semantic memory; eligible when identity/progress agree and neither prose nor memory extends past the target.
- v3: state and graph checkpoint plus exactly the target chapter and its memory chunks. Exact state/chapter/graph/memory checks make this the preferred rollback format and remove repeated full-book prose from every new snapshot.

`StateManager.snapshot_status()` is the single rollback eligibility check used by
the API and mutation path. The UI retrieves all statuses, labels exact/approximate
points, and disables missing, corrupt or progress-mismatched history. A syntactically
valid old snapshot can remain importable for archival fidelity while still being
unusable for rollback; the archive validator reports that condition as a warning.

Existing projects remain readable. Their first post-upgrade generated chapter establishes a legacy checkpoint. Chapters generated after that checkpoint are fully event-sourced. If reliable chapter-zero history is absent, chapter-one regeneration stays blocked until the author-confirmed repair flow establishes an explicit baseline. If an older chapter is manually changed, the UI reports that state reconciliation is required rather than inventing a diff and offers sequential state-only extraction when the preceding timeline is trustworthy.

## Backup boundary

Imports are staged before becoming visible and reject traversal, Windows path tricks, duplicate case-insensitive names, symbolic links, excessive entry counts, abnormal compression ratios, oversized members/expanded archives and a project-ID mismatch inside `project_state.json`. Extraction counts the bytes actually written rather than trusting zip metadata. Staged data is schema-validated across aggregate state, active chapter metadata/bodies, graph, memory, active state events, retired repair timelines and snapshots; retired manifests must match the archived filenames, sizes and SHA-256 values. The staged project is then loaded through both full and lightweight `StateManager` paths before atomic publication. Export archives are temporary and deleted after the HTTP response completes.

Import publishes while holding the destination operation and snapshot locks and updates its revision. Export holds a consistent project snapshot, rejects external file links, and cleans its temporary archive on failure.

Incoming bodies are bounded before FastAPI parses JSON or multipart data. Ordinary
JSON endpoints allow 1 MiB; the three prose mutation routes allow 16 MiB. The
backup route permits 500 MiB of file data plus 64 KiB multipart overhead, permits
exactly one `file` part, caps accumulated multipart headers, rejects ambiguous or
invalid `Content-Length`, and still counts actual ASGI chunks when the header is
missing or understated. Upload reads have a 30-second inter-chunk and 300-second
total deadline. Overflow, disconnect, incomplete EOF, cancellation and malformed
multipart paths close partial spools. Archive validation runs in a worker thread
against that existing bounded spool, avoiding both event-loop blocking and a
second 500 MiB temporary copy.

Every stored JSON read accepts only a regular file, rejects links/reparse points
and special files before opening, rechecks the opened descriptor, and defaults to
a 128 MiB cap. Archive and trash callers use their smaller domain-specific limits.
These are fail-closed corruption/resource boundaries; a legacy JSON aggregate
larger than the cap must be inspected and migrated deliberately, not loaded into
the Web process implicitly.

Read-only migration audit tooling copies real projects into a temporary root, runs
full/light loads and HTTP smoke tests there, compares chapter JSON/TXT against any
pre-sharding backups by SHA-256, and then proves the original source-tree hashes did
not change. Validation never turns a real project into a test fixture.

## Runtime configuration transaction

Configuration updates reserve the same local mutation slot used by generation and
are serialized at the endpoint. One transaction updates the dotenv file,
`os.environ`, validated settings and the live LLM client credential lease. If client
swap or validation fails, every layer rolls back to its prior value. API keys are
never returned by configuration endpoints or written to logs.

The no-op comparison runs inside the same endpoint lock. Equivalent saves do not
rewrite dotenv or swap clients. Updates preserve unrelated lines, BOM and original
newline style; rollback restores exact original bytes, including CRLF and missing
final newlines. The browser disables configuration fields/save until the latest
load succeeds and ignores stale responses from a closed/reopened form.

`scripts/serve_demo.py` is a separate disposable server for UI QA: projects,
custom compliance words and dotenv all use a temporary root; SDK construction and
model transport are replaced with an offline stub. Normal server behavior is not
patched outside that process. Only loopback binding is offered. The original
seed script is not an equivalent isolation boundary.

## Deployment boundary

The current product is a local, single-user authoring application. The server deliberately refuses non-loopback binding because its API can edit projects and configure an API key. Host and Origin checks also reject DNS-rebinding and cross-site browser requests; security headers prevent framing and permit only local scripts/styles/fonts, including on rejected requests. Stateful responses are marked `no-store`/`no-cache`: a cached bookshelf, revision, task state or recovery decision must never outlive the underlying files. Tailwind output, Cytoscape and Font Awesome assets are versioned under `web/assets/`, so the UI has no runtime CDN dependency. If the product becomes multi-user, the correct next architecture is a database plus object storage, authenticated APIs and a durable external job queue—not additional global dictionaries or filesystem locks.

## Verification

```bash
uv run --isolated --no-project --with-requirements requirements-dev.lock python scripts/check.py
# Optional online dependency vulnerability audit:
uv run --isolated --no-project --with-requirements requirements-dev.lock python scripts/check.py --audit
```

The gate runs Ruff, medium/high-severity Bandit checks, compilation, pytest with
branch coverage (including CLI and HTTP entrypoints), strict HTML structure checks,
dependency consistency, CLI help, checked-in Web-asset checks, actual-script
JavaScript state regression tests and syntax checks when Node.js is installed,
and an optional published vulnerability audit of both exact hashed Python lock
files. Tests use temporary project roots and real child-process lock
contention; they do not make paid model calls.

The gate fingerprints its source, asset and lock inputs at startup, checks them
after every step, and refuses success if they change. An atomic, per-Python-version
report in `.quality/check-py<major>.<minor>.json` records input hashes, check outcomes,
timestamps, skipped work and overall status. Missing Node.js produces a `partial`
report, never a fully verified one. An interrupted run remains marked `running`;
that file alone is not evidence that its process is still alive. The report never
hashes `.env` or user novels and is excluded from version control.

Each invocation assigns `COVERAGE_FILE` to its own temporary directory, including
child-process fragments. Concurrent Python-version gates cannot erase or combine
one another's coverage data or an existing local coverage database. The directory
is cleaned after success/failure without changing the parent environment.

`.github/workflows/quality.yml` runs that exact locked gate for Python 3.11 and
3.14 on GitHub-hosted Ubuntu, macOS and Windows runners. A separate Node job uses
`npm ci --ignore-scripts`, audits high-severity advisories, runs the browser-state
suite, rebuilds `web/assets`, and rejects any generated diff. A dedicated browser
job installs locked Chromium, Firefox and WebKit engines and runs the real page
against the disposable offline server while rejecting non-loopback requests.
Repository permission is read-only; every external Action is pinned to a full commit
SHA, checkout drops credentials, and model-provider variables are forced to an empty
key plus a closed loopback endpoint. Workflow and Dependabot policy files are
themselves gate inputs.
Local contract tests and static workflow validation establish configuration
integrity, not successful execution on an OS; only completed hosted runs provide
platform evidence.

Lower-level commands remain:

```bash
python -m compileall -q main.py web_ui.py api services agents config core pipeline examples tests
python -m pytest -q
python -m pip check
```

Web dependencies and generated offline assets are reproducible from the Node lock:

```bash
npm ci
npm run build:web
npm run test:web
npm audit
```

The real-browser smoke gate is separate from the minimal-DOM state suite. It starts
`scripts/serve_demo.py` on an explicit loopback port, installs the locked Playwright
engines, and exercises Chromium, Firefox and WebKit against disposable projects:

```bash
npx --no-install playwright install chromium
npm run test:browser
npm run test:browser:all
```

The CI job installs all three engines with their host dependencies and keeps the
browser cache and output directory under the runner's temporary root. The fixture
forces an empty model key, a closed loopback endpoint and no real-project path; a
browser run is evidence for UI/runtime behavior only, not for model quality. The
three-engine flow also round-trips a generated ZIP through deletion and the real file
input, verifies restored prose, executes a chapter rollback, restores the shared
two-chapter fixture from backup, and restores a second work from local trash. These
destructive operations are confined to the disposable server root.

The same fixture replaces only the explicit model boundaries with deterministic
objects and leaves checkpoint publication, outline validation, the background worker,
chapter assembly/audit, state mutation and UI polling on their production paths. A
browser creates a work, publishes the three planning checkpoints, validates a ten-chapter
outline, generates and opens chapter one, continues through chapter three, observes the
automatic delivery package, changes chapter two's outline and rewrites it, verifies that
the now-stale package and its files disappear, then explicitly rebuilds the package before
removing the work for the next engine. Delivery is a derived projection: changes to its
concept, world, protagonist or chapter-source inputs invalidate it transactionally, while
an identical chapter republication does not. The underlying transport remains a fail-closed
guard, so an unpatched model path fails the gate instead of reaching a provider.

Full-project search is an explicit read-only projection in `core/project_search.py`.
The HTTP endpoint bounds query and result sizes, returns the exact project revision,
and searches lazily stored chapter prose plus structured character, foreshadowing,
outline, world and concept data. The command palette debounces and cancels superseded
requests, escapes every returned label/snippet, and accepts a response only while its
project identity, load token and revision still match the open workspace. Search never
publishes state or advances the revision.

The UI exposes a skip-to-content link, named library/chapter landmarks, native
buttons for every state-changing or content-locating action, and a contextual
workspace region whose accessible name follows the selected work. Compliance
findings retain their word identity when locating the matching paragraph. Static
markup tests reject duplicate ARIA attributes, unresolved references, heading
jumps in dialogs, and insufficient theme-token contrast; the real-browser check
also verifies the resulting accessibility tree and console state.

See `OPTIMIZATION_AUDIT.md` for verified work and explicit remaining work. Passing
this gate alone does not establish real model judgment quality or untested
platform/filesystem durability guarantees. Isolated browser checks cover recovery,
reader/theme rendering, dialog focus and a two-tab edit conflict without using
real projects. Mobile checks cover 320/390px layout and native downloads; the locked
three-engine suite covers ZIP import, destructive rollback and trash restoration,
while API tests additionally reject an existing imported project name. The closed
mobile sidebar is invisible to keyboard/accessibility navigation, its toggle
exposes expansion state, and Escape returns focus. This is focused evidence, not
an exhaustive responsive or screen-reader matrix. Slow/reordered response tests execute the production inline
JavaScript against a minimal DOM; they complement but do not replace real-browser
checks. Tailwind 4 assets target modern browsers (Chrome 111+, Safari 16.4+,
Firefox 128+); older browsers are not claimed supported.
