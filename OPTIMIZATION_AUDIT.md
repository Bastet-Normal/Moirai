# Optimization audit

Updated: 2026-09-01. Objective: 全方面无死角自主决策完善优化.
Status: **active**, not a claim that all optimization work is complete.

## Verified this pass

- Real thread and child-process contention rejects a competing writer before model invocation. Web/CLI jobs own the complete workflow; reads remain available between disk writes.
- Version tombstones prevent stale state overwrites and stale deletion/resurrection after rename, delete and import. Lightweight and corrupt views are read-only.
- A regression reproduced rollback that looked correct in memory but reloaded edited prose. Retained chapter JSON/TXT and metadata are now restored too, and tested through a new StateManager.
- Historical event replay remains chronological across chapters 999/1000.
- Web generation writes per-chapter success/failure records and stops at the first failed chapter; task slots become terminal after expected errors.
- Backup publication is locked, exceptions close archive handles, export rejects links outside the project and cleans temporary files on failure.
- Test fixtures are isolated from user projects. The two legacy test suites no longer delete a fixed `test_output` folder or write fixtures under the actual output root.
- Ruff, pytest-cov and pip-audit are declared and executable. Runtime dependency declarations have major-version bounds. `scripts/check.py` provides a repeatable gate, including CLI and browser JavaScript syntax.
- A least-privilege hosted workflow now defines the locked gate across Ubuntu, macOS and Windows with Python 3.11/3.14. All Actions are pinned to full commit SHAs, checkout credentials are discarded, provider access is disabled, and a separate Node job audits and deterministically rebuilds checked-in assets. Dependabot covers Action and npm updates monthly. Contract tests and `actionlint` validate the configuration; no hosted run is claimed yet.
- Multi-file mutations now have bounded, durable undo journals. Fault tests inject failures after every chapter file publication and real child-process exits before/after commit and during recovery. Pre-commit recovery restores the exact old file set; committed writes survive lost acknowledgement and restart.
- Interrupted rename/delete are discoverable even when the original project name is absent. Readers do not undo live writers; imports recover a pending delete before deciding the name is available.
- Recovery checks path confinement and every original-file checksum before changing files, checkpoints completed undo operations, and keeps damaged journals for inspection. Windows case aliases no longer bypass journaling or change restored filenames.
- Deletion now retains a recoverable copy in the local trash directory. Rename cleanup targets only known generated exports, preserving unrelated research files with the old project-name prefix.
- Historical writing now reconstructs an isolated pre-chapter state from complete event history or an exact legacy checkpoint. Future levels/items/deaths/hooks/summaries and current/future memory are excluded. Static editorial overrides remain usable without retroactively applying undated dynamic changes.
- Missing/mismatched history or unresolved earlier state fails before model invocation. Writer and critic both receive the factual context; the writer previously accepted but omitted its character-state argument from the prompt.
- Draft selection prioritizes actual passes over scores and preserves reviewed drafts during critic outages. Changed polishing is reaudited, empty prose is rejected, and extraction receives the exact accepted final text.
- Locally stamped text digests bind audit reports to their drafts. Full loads/saves verify the final report; metadata/API/reader show its status instead of the last attempt. Manual edits and unverifiable legacy reports cannot retain a stale passed label.
- Extraction-failure markers now commit atomically with prose. A fault test proves that failure to write the marker rolls back the entire generation. Downstream writing stops until earlier state is reconciled.
- The LLM transport has an application-owned retry budget, closes failed streams, honors bounded `Retry-After`, rejects truncated/oversized output, preserves JSON string values, downgrades unsupported parameters only on explicit provider errors, and swaps credentials through concurrency-safe leases. Twenty-four transport tests include actual SDK SSE parsing without external model calls.
- Full-book Markdown/TXT exports are coalesced instead of rebuilt after every chapter save. A 1,000-chapter, 2.5-million-character benchmark completed in 3.253 seconds and produced about 7.5 MiB of output on this host.
- Archive extraction now counts actual streamed bytes and validates every staged aggregate, active chapter, metadata shard, graph, memory shard, state event and snapshot before atomic publication. Partial legacy metadata is completed from canonical chapter shards rather than hiding chapters.
- Deleted projects can be listed and restored in-app. Restore validates trash identity, refuses conflicts, participates in task reservation/project locks, and was exercised end-to-end in an isolated browser project root.
- Runtime configuration is a transaction spanning dotenv, process environment, validated settings and the live LLM client. A failed client swap restores all layers; concurrent config/generation requests are serialized.
- Tailwind, Cytoscape, Font Awesome CSS/fonts and licenses are checked-in offline assets. CSP no longer allows third-party resource hosts or `unsafe-eval`; an actual browser load completed without console warnings/errors. `npm audit` reports zero known vulnerabilities.
- Rollback eligibility is explicit per snapshot. Exact v3 points are preferred, compatible old points are labelled approximate, and corrupt/progress-mismatched history is disabled in both API/UI and rechecked by the mutation path.
- Runtime and development dependency graphs are committed as universal Python 3.11+ lock files with hashes. Fresh isolated lock-based gates run independently under Python 3.11 and Python 3.14; the same lock has also passed under Python 3.12.
- Editorial/destructive API requests require the loaded project revision (428 if absent, 409 if stale). A body and its revision are read under one snapshot lock. The worker rechecks queued regeneration before invoking a model; locked StateManager validation still covers concurrent changes after the HTTP check.
- Editors retain project/chapter ownership, ignore late responses and keep unsaved input on conflict. Forty-five tests execute the production JavaScript with a minimal DOM: same/different-book races, chapter metadata/body revisions, rewrite candidates, history-repair ownership, recovery/audit state, effective-chapter acknowledgements, old save acknowledgements, non-overlapping polling, config load/save races, preserved tab/chapter/scroll state, graph color compatibility, native exports, palette configuration access and keyboard behavior. Tab arrow events are consumed rather than bubbling into chapter navigation; chapter shortcuts also respect the active tab, form/contenteditable focus, modifiers and loading state. A real two-tab browser test saved B's draft, then verified A's stale submission was rejected while its draft remained in the editor.
- Configuration no-op comparisons occur inside the update lock. Equivalent saves do not rewrite dotenv or swap clients. Exact-byte rollback tests cover CRLF, BOM, blank lines and no final newline. Browser fields/save remain disabled until the current configuration load succeeds.
- `scripts/serve_demo.py` isolates projects, dotenv and custom words, replaces SDK construction and blocks model transport. Its deterministic local state extractor can exercise the repair publication path without a provider; a child-process test completes two chapters, proves the prose unchanged, exercises config changes and then proves ordinary model calls still fail offline. Custom compliance words now honor OUTPUT_DIR instead of always writing the real library.
- Tailwind assets now use v4.3.3. Browser checks cover reader rendering, dialog focus return/Tab loops and command selection. A markup regression introduced during the fieldset change was caught in the real browser, repaired, and guarded with two strict tag/ID/dialog-boundary tests. This is why stubbed JavaScript tests alone are not treated as browser verification.
- Bandit medium/high-severity checks are part of the gate. New graph relation IDs use SHA-256; existing saved IDs are not rewritten. Low-severity findings are not blanket-suppressed.
- Vulnerability scanning now audits both exact hashed lock files, not only runtime requirement ranges. It found pytest 8.4.2's PYSEC-2026-1845; the developer requirement now needs >=9.0.3 and the lock resolves 9.1.1. The [pytest release notes](https://docs.pytest.org/en/latest/changelog.html#pytest-9-0-3-2026-04-07) document the temporary-directory fix. Both locks passed the rescan.
- API inputs now use strict, extra-forbidden contracts with field/list/text bounds and Unicode/NUL checks. Invalid generation counts are rejected instead of clamped; custom compliance categories/words are rejected instead of silently truncated. Validation responses omit submitted values so keys and prose are not echoed.
- Raw requests are bounded before JSON/multipart parsing. Tests cover declared, missing, duplicated, invalid and understated lengths; exact boundaries; streamed overflow; idle/total timeouts; disconnect; incomplete EOF; cancellation; excessive headers; malformed multipart; and partial-spool closure. Backup validation reuses the parser spool in a worker thread rather than blocking the event loop or making a second full-size copy.
- Chapter writes enforce the 2,000,000-character limit in StateManager as well as the API. Paragraph/compliance growth is calculated before allocating the expanded string, and fault tests prove oversize attempts leave every project byte and revision unchanged.
- Stored JSON reads are restricted to regular, non-reparse files and 128 MiB by default, with smaller archive/trash limits. Exact-size, oversize, directory, FIFO, parse-failure and platform-permitted symlink paths are tested.
- Recovery discovery now returns path-free structured diagnostics instead of turning one corrupt header into a bookshelf 500. Known participants are isolated; unknown ownership freezes publication globally. New/import/trash-restore paths cannot bypass the freeze. Non-regular roots/logs, oversized markers and injected recovery I/O errors all preserve project bytes and evidence.
- Chapter replacement merges old machine-audit and human-review history. The UI exposes issue/directive history and an author decision bound to the exact prose SHA-256; any later edit invalidates current approval without deleting history. Review writes retain semantic-memory metadata without loading or rewriting memory shards, require the current project revision, escape rendered content and ignore late cross-project acknowledgements.
- Normal local responses carry explicit no-store/no-cache policy and the security headers also cover rejected Host/Origin requests. A real browser fault fixture reproduced stale bookshelf JSON hiding a new global recovery freeze; after the fix the alert appeared immediately, import/new-project actions were blocked, diagnostics remained readable and the console stayed clean.
- Lightweight human-review writes fail closed when chapter metadata is missing but a memory shard exists, rather than guessing a zero count. A byte-for-byte fault test proves that the refusal leaves the project untouched.
- Character-card dynamic edits now carry an explicit effective chapter in a bounded versioned history. Replay applies each override between the corresponding chapter events, so it affects the intended next-chapter context but a later story event can supersede it; rollback excludes future edits. Static editorial fields remain historical, legacy undated dynamic values remain current-only, repeated replay parses the override file once, and corrupt override/reconciliation records put the whole loaded view into read-only protection.
- Independent regression cases reproduced and fixed older overrides defeating later checkpoint state, legacy undated values defeating dated replacements, unchanged form fields accidentally becoming new events, and writes larger than the next reader accepts. The pure `character_overrides` contract preserves extra legacy evidence, validates timestamps/types/order/retirement and enforces matching read/write size limits. Rolled-back future edits remain as inactive evidence and cannot revive on a new branch.
- Reconciliation chapter identities are strict positive integers: booleans, negative/fractional values and malformed containers are not coerced or silently discarded. Invalid markers make the loaded view read-only instead of suggesting that no reconciliation is needed.
- The gate records per-runtime input fingerprints and step outcomes under `.quality`, detects changed source after each check, and distinguishes partial/running results from completed verification. Dedicated tests cover source changes, skipped browser checks, timeouts, launch failures and exclusion of secrets/user data from hashing.
- Coverage databases and child-process fragments now belong to a unique temporary directory per gate invocation. Review of the first parallel run found that the former shared `.coverage` path could mix evidence; those coverage figures are not the final evidence. Success/failure tests verify isolation, preservation of an existing database/environment and cleanup. Final dual-runtime gates are rerun with isolated coverage.
- An isolated real-browser character edit saved a background-only change and confirmed that `dynamic_history` stayed empty; a subsequent level change produced only that field's event at chapter 2 and displayed the matching effective-chapter acknowledgement. The page logged no warnings/errors and its disposable root was cleaned up normally. The navigation reset exposed by this check has now been repaired and retested.
- Project refresh preserves the editing tab, selected chapter identity, expanded directory and scroll positions; late lazy-body responses cannot restore an old chapter's position. Explicit book selection still resets to the reader. A new isolated browser run saved a character without leaving its tab, saved chapter 2 paragraph 10 with reading progress unchanged at 54%, and used End/Home to switch tabs and return to the same reading position. Tabs expose consistent roles/selection, form controls have labels, and dynamic actions have escaped contextual names. Browser testing also reproduced overlapping manual-edit/AI-rewrite buttons: the controls now occupy separate targets and manual editing opens the correct dialog. No warning/error console entries were observed. Five strict markup tests complement the production-script tests; neither substitutes for complete accessibility testing.

- A separate browser check confirmed that ArrowRight from the reader tab selects the graph tab without advancing chapter 1. This exposed unsupported eight-digit graph edge colors; they were replaced with six-digit theme colors and verified against the actual shipped Cytoscape renderer in both themes. That color correction has renderer-level regression evidence, not a subsequent visual browser comparison. Its temporary server/tab were closed normally and its root was automatically removed.

- A locked Playwright 1.62.1 smoke suite now runs the actual page in Chromium, Firefox
  and WebKit from a disposable `serve_demo.py` server. It checks response security
  headers, named workspace landmarks, the expanded compliance panel and focus return,
  exact paragraph highlighting for the second finding, four supported viewport widths,
  and keyboard tab navigation without chapter drift, while rejecting any HTTP(S)
  request outside loopback. The local Windows run passed all
  24 engine/scenario combinations, including TXT export revision preservation,
  revision-bound chapter editing, and a real ZIP backup/delete/import/rollback/trash-restore
  round trip across Chromium, Firefox and WebKit. Each engine also creates a new work,
  publishes the real staged planning checkpoints, validates a ten-chapter outline, runs
  the background worker through first-chapter audit/state publication, refreshes the shelf,
  continues through chapter three, observes automatic commercial delivery, changes chapter
  two's outline and rewrites it, proves that the stale package disappears, explicitly rebuilds
  delivery, opens the generated prose and restores the active fixture count; the CI job installs and
  repeats the full matrix on hosted Linux. Firefox's own precisely matched sticky-position diagnostic is excluded,
  while every application warning, console error and uncaught page error still fails. This is
  focused runtime evidence, not a claim that real model judgment, every continuation/delivery
  workflow or screen reader is covered.

- Legacy history repair is now explicit and author-controlled. Candidate `initial_state` values are not selected automatically; the confirmed chapter-zero baseline contains only chosen characters, and the prior timeline is retained with a size/SHA-256 evidence manifest. Active and retired timeline identities, ordering and archive evidence are validated on load/import instead of silently repaired.
- The reader now exposes a state-only action for exactly the first pending chapter. It sends the exact saved prose to the state agent, never the writer, and publishes the event/state/graph/snapshot atomically while preserving prose and review history. Version-2 events bind extraction to the prose SHA-256; stale, out-of-order, missing-history and model-failure cases stop before partial publication. The queued worker repeats the revision check before any model call.
- An isolated real-browser run initialized a chapter-zero baseline, reconciled chapters 1 and 2 in order, and ended at `0` pending events with both bodies byte-for-byte unchanged and both event hashes matched. It also exposed a CSS cascade that made `.btn.hidden` and `.chip.hidden` visible; the scoped rule and markup regression test now enforce the intended state. Confirmation text discloses that saved prose is sent to the state model and may incur cost. The page logged no warnings/errors, and the tab, server, disposable directory and port were all cleanly released.
- Mobile browser checks reproduced a 558px-wide header on a 390px viewport and a 64px-wide reader on a 320px viewport. Responsive header controls and a horizontal directory above full-width prose now keep both tested widths within the viewport. Model configuration remains available through the command palette when a book is open. Closed mobile sidebars no longer retain invisible keyboard targets; expansion state and Escape focus return are tested.
- Four export actions now use native same-origin attachment links, not popup tabs. A browser sequence captured TXT, Markdown, EPUB and ZIP download events while remaining on the same page. It exposed that publication GETs invoked editorial mutations and invalidated the open workspace revision. Those routes now render a locked prose snapshot into per-request temporary files, without changing project bytes, mtimes or revisions; the same browser sequence now opens valid rollback choices without refreshing. The front-end download names now match the server contract (`*_全本.md`, `*_全本发布稿.txt`, `*.epub`, `*_backup.zip`). Production-script tests cover all four names, while the three-engine matrix confirms the TXT name and unchanged project revision.
- Twenty-six download regressions cover original-editor validity, response cleanup, concurrent editing during snapshot rendering, independent request paths, partial-render failure, cancellation/disconnect, ranges/HEAD, corrupt/missing/empty projects, and a downloaded ZIP round trip through the import API. Existing-name import is rejected without overwriting the source. ZIP downloads share the response-lifetime cleanup while retaining source locks and link checks. Abrupt process death may still leave temporary artifacts.
- The earlier mobile pass only inspected and cancelled the rollback dialog. The current
  three-engine gate now downloads a real generated ZIP, moves the source to local trash,
  imports the ZIP through the hidden file input, verifies the original prose, performs a
  destructive rollback to chapter 1, restores the two-chapter baseline from that backup,
  and restores a second project from trash. Every operation remains inside the disposable
  offline server root, and the test restores shared fixture state before the next engine.
- CLI workflow regressions reproduced `auto` silently reusing an existing project despite a new idea. `auto`/`plan` now require a new identity, existing chapters require explicit `--rewrite`, and invalid ranges are rejected before initial model planning in CLI/pipeline/worker paths. Batch failures expose the stopped chapter and completed count, preserve the successful prefix and return nonzero exit status; fresh continuation starts after that prefix. Unexpected CLI exceptions no longer dump provider details or local variables.
- Initialization regressions reproduced partial concept/world publication and new characters starting with model-default current state instead of their planned initial state. Complete initial settings and bounded retry dimensions now publish in one transaction; new current states are detached copies of the planned initial values. Duplicate/blank cast identities fail before publication. Failed total-outline generation remains resumable without replacing the saved package, using `resume-plan` or normal CLI/Web continuation. Outline publication and clearing the pending marker are atomic.
- Initial-outline checks now enforce the actual prompt contract, including ordered macro volumes, a continuous first-volume prefix, unique/ordered chapter identities and coherent volume/pacing references. Later volumes may remain macro-only for rolling planning. Recovery refuses completed projects and changed dimensions; legacy partial settings require explicit original dimensions. Pending checkpoints round-trip through validated backup import, and corrupt request types are refused.
- Two new real child-process crash tests cover failure during initial-package publication and termination during the outline model call. Restart either restores the empty pre-publication project or retains the complete resumable package. CLI/API recovery tests use deterministic model-stage doubles and actual project persistence; the continuation disclosure is tested against production JavaScript, not claimed as a new browser end-to-end run.
- Rolling-planning regressions reproduced later macro volumes never being selected, ten-chapter blocks crossing volume or existing-outline boundaries, and malformed extensions being silently normalized. New outlines declare continuous per-volume chapter ranges. Planning now selects the true owner, clips at both boundaries, supplies adjacent-volume/final-climax context, and atomically publishes only complete one-to-one chapter/pacing blocks. Invalid, failed and argument-mutating model doubles leave the project unchanged; child-process crashes before/after commit recover either the old or complete new outline. Existing legacy outlines remain byte-compatible and are never auto-migrated; ambiguous empty legacy volumes fail instead of guessing.
- Concept-planner checkpoints now commit the original bounded request before any model call, then persist concept, world and cast separately. Deterministic failures and real child-process exits at each model stage prove that restart begins at the first missing result. Complete-cast promotion is separately atomic and retries without another planner call after a publication fault. CLI `resume-plan`, CLI continuation and Web continuation recover every stage; partial projects remain visible and read-only, disclose model cost, and keep full backup round trips. Corrupt order, request types, mixed canonical/checkpoint state and hostile checkpoint fields fail closed without publication.
- Unexpected-error boundaries no longer copy provider responses, raw model output, exception notes, prose, credentials or untrusted archive member names into terminals, HTTP responses, background status, project generation reports or ordinary error logs. A reusable 12-hex diagnostic ID links model transport, pipeline, worker, CLI and Web layers. Logs retain only exception type chains, source basenames/lines and hashed string context. Adversarial regression fixtures inject credential-like strings and unpublished prose through every boundary and assert their absence.
- Real-browser recovery QA now distinguishes concept-stage partial projects with their project ID across the shelf, workspace and confirmation dialog. Zero-chapter actions are natively disabled, backup remains available, and continuation discloses the exact missing stage and model-cost boundary. Responsive checks at 1024px and 768px cover the overlay shelf, wrapped controls and bounded modal; keyboard tab navigation scrolls the focused destination into view without moving the page vertically. Browser console inspection returned no warnings or errors.
- Accessibility hardening added a skip-to-content link, named library/chapter/workspace landmarks, native buttons for title and compliance navigation, contextual workspace labels, and no duplicate ARIA attributes. A real-browser accessibility-tree audit found no unnamed visible focusable controls, no hidden focusable leakage, no unresolved ARIA references, and no console warnings/errors. The risk summary now exposes its expanded state, opens the named reader/compliance panel and moves focus into its findings; activating the second finding highlighted paragraph 10 rather than the first match. Theme-token contrast tests cover both palettes, primary-button endpoints, and dialog heading levels.

Evidence is distributed across the deterministic transport, infrastructure,
generation-integrity, archive, trash, runtime-config and Web/API test modules;
`scripts/check.py` keeps the full scope in one repeatable command. Test counts are
not a substitute for checking the invariants those tests actually exercise.

Latest isolated gate: **549 passed, 2 skipped, 31 subtests passed**;
**45 JavaScript tests pass**. Ruff, Bandit medium/high severity and compilation are clean,
dependency consistency clean, CLI contract successful, checked-in assets present,
JavaScript syntax successful, and no known published Python dependency
vulnerabilities in either lock at scan time. Branch coverage is **84% including Web/CLI**. Both
skips are real symbolic-link rejection tests because this Windows account is not
permitted to create their link fixtures; the rejection implementations are still
covered with platform-neutral link/path tests. Starlette's deprecated legacy
`httpx` TestClient path was replaced by its documented `httpx2` backend.

The full gate above ran in a fresh `uv` environment from
`requirements-dev.lock` and selected Python 3.11.15; its persistent report confirms
zero changed inputs and zero omitted check groups. The same current source passed
all 549 tests and 45 JavaScript tests again under Python 3.14.6, also with unchanged
inputs. The reports are `.quality/check-py3.11.json` (including both vulnerability
queries) and `.quality/check-py3.14.json` (offline gate). The same lock has also
passed an earlier source revision under Python 3.12.13.
The final dual-runtime run uses separate coverage databases; both reports match
the same 105 current input hashes with no omitted check groups. The final reports
supersede earlier incomplete environment probes. A full Node dependency audit also returned
zero known published vulnerabilities.
The host Python 3.14 environment is also part of the regression workflow. No model
provider was called and no API billing was incurred.

Read-only checks of `demo_showcase` (3 chapters) and `e2e_trial` (9 chapters)
validated 115 source files, successful detail/chapter APIs and gzip responses.
All 24 chapter JSON/TXT files exactly match pre-sharding migration backups by
SHA-256, and complete source-tree hashes were identical before/after. One old
`demo_showcase` v2 snapshot claims chapter 2 while containing progress through
chapter 3; it is retained as historical data but now reported and disabled as an
unsafe rollback point. Neither real project was modified.

## Remaining work (do not treat these as verified)

1. **Platform and storage evidence.** Python 3.11/3.12/3.14 and Windows process-crash
   recovery are exercised. A hosted Ubuntu/macOS/Windows CI matrix is defined and
   locally linted, but no hosted run is available in this non-Git workspace; POSIX execution, physical power
   loss, disk damage, hostile external edits and unusual/network filesystems remain
   unverified; do not generalize current durability evidence to them. Corrupt
   journals now fail closed with guided, path-free diagnostics, but actual expert
   repair or deletion remains deliberately manual and backup-first.
2. **Downstream narrative consequences.** Author-confirmed chapter-zero repair and
   sequential state-only extraction are implemented and browser-tested. Replaying
   later saved deltas or rebuilding them from existing prose does not itself prove
   that later prose remains narratively consistent after an earlier rewrite; that
   requires representative corpus review and author judgment.
3. **Real model quality and review UX.** Deterministic workflows and SDK-level
   local streams verify transport, draft,
   polish, audit and extraction identity, including unavailable services. This
   does not establish model judgment quality. Audit history and text-bound human
   decisions are now reviewable, but no representative human scoring corpus or paid
   provider comparison was run this pass.
4. **Browser breadth and accessibility.** Offline rendering, startup, recovery,
   two-tab edit conflicts and key dialog interactions are browser-tested in
   isolation. A locked real-browser smoke matrix now covers the disposable library,
   workspace landmarks, security headers, compliance focus/paragraph navigation,
   keyboard tab switching and 320/390/768/1024px layout across Chromium, Firefox and
   WebKit on Windows. The hosted Linux matrix repeats the same suite but is not yet
   claimed executed. Production-script tests now cover important
   late-response/polling races and HTML tests guard dialog boundaries. A complete
   automated browser matrix for create/plan and every read/edit tool, screen readers,
   rolling-outline extension across volume boundaries, every read/edit tool, screen readers,
   mobile layouts beyond the checked widths, slow assets and all keyboard paths is still
   absent. New-work planning, three-chapter continuation, chapter regeneration and commercial
   delivery are now included in all three engines. Editing-tab/reader-position preservation, native navigation controls, tab
   keyboard behavior and explicit form/action labels are now implemented and have
   focused browser evidence.
   The 320/390px mobile reader, sidebar and four-format downloads and the 768/1024px
   recovery layouts have now been checked; other breakpoints, screen readers and the full workflow matrix
   remain unverified.
   This is not WCAG certification.
5. **Scale beyond the local product boundary.** The storage benchmark covers
   1,000 chapters, but million-word semantic relevance and narrative coherence
   require corpus- and human-level evaluation. The current architecture is
   intentionally loopback-only and single-user; public/multi-user deployment would
   require authentication, database/object storage and a durable external queue.

## Operational notes

- No real generation, user-project edits or external publication were performed.
  Real-project smoke checks are read-only; both projects' 115 files remained
  unchanged in the latest repeat audit.
- The latest pass used deterministic offline model-stage doubles,
  including disposable real-pipeline generation and child-process crash fixtures at
  every concept, world, cast, promotion, outline and prose publication boundary;
  plus adversarial exceptions containing credential-like strings and unpublished
  prose across all public error boundaries. No provider was called and no billing
  was incurred. Both final quality reports match all 105 current source inputs with
  zero changed or omitted groups. The
  read-only repeat audit again matched all 115 real-project files and both prior
  tree hashes; the real configuration still matches the checksum below. The focused
  browser check used only the isolated offline server, whose disposable root was
  removed on shutdown; port 8766 has no listener.
- During an earlier keyboard check, Save Config was accidentally clicked on the
  real server. The key field was blank and no key replacement occurred; the form
  used the existing effective settings. `.env` formatting was normalized from 603
  to 599 bytes at 2026-08-30 09:33:09 UTC. No exact pre-click byte copy exists, so
  formatting was not guessed/restored. This was disclosed to the user. Subsequent
  work uses the fully isolated offline server above; the real file has not changed
  since that incident. Do not describe the whole optimization run as config-untouched.
- Dependency vulnerability scans report the published advisories available at scan
  time; a clean result is not a guarantee of absence of vulnerabilities.
- Lock files make declared dependencies reproducible, not model responses or OS
  filesystem behavior. Regenerate and review both Python locks whenever requirement
  ranges change; use `npm ci` for the checked-in Node lock.
- The app remains local and single-user. This audit does not authorize deploying
  its write/config endpoints to a public interface.
- Final cleanup revalidated every target boundary and moved the two legacy test
  fixtures plus all five remaining `novelengine-*` temporary roots to the Windows
  Recycle Bin. The temporary roots were `novelengine-browser-5b5944a0b1014202866800117e557af6`,
  `novelengine-tailwind4-dc4154b0f16e41f6a5f532f7af4b4c55`,
  `novelengine-offline-demo-irmku44n`, `novelengine-offline-demo-kpnf2o__` and
  `novelengine-offline-demo-s7u3r50k`. The operation is recoverable; neither real
  project nor `.env` was in scope, and no `novelengine-*` temporary root remains.
- The latest audit/recovery browser server and its tab were closed normally; both
  disposable roots, including the intentionally corrupt recovery fixture, were
  removed automatically on shutdown.
- The subsequent navigation/accessibility browser server also shut down normally;
  both of this run's tabs were closed and its temporary root was automatically
  removed. The real `.env` still has the post-incident size and timestamp above.
- The latest history-repair browser run also closed its tab/server normally; its
  disposable root was removed and port 8766 has no listener. The final read-only
  project audit left all 115 real-project files byte-for-byte unchanged. `.env`
  remained 599 bytes with SHA-256
  `9D38F6F1645C3CCB957BBEAB853A9059E6C42457A02840532D993D1707B969EA`.
- The mobile/export rerun closed its browser tab and shut down its server normally.
  Both `novelengine-offline-demo-s2zmd98a` and
  `novelengine-offline-demo-9g_63xuy` were removed automatically, and port 8766 has
  no listener. The real configuration still matches the size, timestamp and checksum above.
  Both real-project tree hashes also match the preceding pass: SHA-256 of sorted
  Windows relative paths (including a leading backslash), `|`, uppercase file
  SHA-256, joined by LF. `demo_showcase` is
  `B6A2A18D5CD7F4B00068B4C3CA08D2A6E499E50BACE97B87B321E5FF11A97180`;
  `e2e_trial` is
  `F6A440361A39C2A6B87C6A135DCDC71EB56426EDA476042DF613535A56F11031`.
- The latest cleanup rechecked the temporary-directory boundary and absence of live
  listeners, then moved two empty quality-output roots and two residual isolated-demo
  roots to the Windows Recycle Bin. All four operations are recoverable; no formal
  project or configuration file was in scope, and no matching quality/demo root remains.
- The preceding continuation aligned front-end download names with the server contract
  for Markdown/TXT/EPUB/ZIP. Node state tests (45), the focused Python checks (21),
  and the real Chromium/Firefox/WebKit smoke matrix (18/18) passed; fresh Python
  3.11 and 3.14 gates reported 549 passed, 2 skipped and 31 subtests, with 105/105
  unchanged inputs and no omitted checks. The four disposable demo roots, the
  Playwright browser cache and `test-results` were moved to the Recycle Bin after
  verifying 8766-8769 had no listeners; formal projects and `.env` retained their
  recorded file counts, hashes, size and timestamp.
- The preceding continuation expanded the locked three-engine gate to 21 combinations.
  Each engine downloaded a generated backup, deleted and re-imported the project through
  the real file input, verified restored prose, rolled back to chapter 1, rebuilt the
  shared two-chapter fixture from the same ZIP, and restored a second work from local
  trash. The first matrix exposed both an upload-fixture filename mistake and cross-engine
  state leakage; explicit ZIP metadata and end-of-test baseline restoration fixed those
  test defects. The final Chromium/Firefox/WebKit run passed 21/21 with no application
  warning, console error, page error or external HTTP(S) request. Fresh Python 3.11 and
  3.14 gates again reported 549 passed, 2 skipped, 31 subtests and 84% branch coverage;
  both reports match all 105 current inputs with no changed or omitted checks, both Python
  locks and the Node lock report no known vulnerabilities. Four residual disposable demo
  roots and the successful `test-results` directory were moved to the Recycle Bin after
  confirming ports 8766-8769 were idle. The formal project counts/tree hashes and `.env`
  size/timestamp/hash remained unchanged; the locked Playwright cache was retained for
  reproducible local reruns.
- The preceding continuation extended the same locked matrix to 24 combinations with a
  successful create-and-generate path in every engine. `serve_demo.py` now replaces only
  the planner, outline, beat, writer, auditor, polisher, state and optional multimodal
  model boundaries with deterministic objects; the production checkpoint publication,
  atomic promotion, ten-chapter outline validation, background worker, chapter assembly,
  audit binding, state mutation and UI refresh remain intact. The lower-level transport
  still fails closed, so an unaccounted model call cannot reach a provider or incur cost.
- The first fast offline run exposed a polling race: a task completing before the first
  1.6-second interval could remain absent from the shelf. Poll startup now establishes the
  task baseline immediately, with a focused state regression. The final
  Chromium/Firefox/WebKit run passed 24/24 with no application warning, console error,
  page error or external HTTP(S) request; the state suite passed 46/46.
- Fresh isolated Python 3.11 and 3.14 gates each reported 549 passed, 2 host-privilege
  skips, 31 subtests and 84% branch coverage. Their reports cover all 105 inputs with
  `changed_inputs=0` and `skipped_checks=0`; the 3.11 audit found no published
  vulnerabilities in either Python lock. Two residual demo roots, two empty quality
  roots and `test-results` were boundary-checked and moved to the Windows Recycle Bin
  (recoverable) after confirming ports 8766-8769 were idle. The retained browser cache
  supports reproducible reruns. Formal projects still contain 38/77 files with the exact
  tree hashes above, and the real `.env` remains 599 bytes with its recorded timestamp and
  SHA-256.
- The current continuation fixed the commercial-delivery projection boundary. A package
  depends on concept, world, protagonist and chapter source fields, but previously survived
  edits to those inputs and could present stale promotional copy and cost estimates as current.
  Source-changing mutations now clear the in-state package and its JSON/script/prompt files
  inside the same project transaction; an identical chapter republication deliberately keeps
  a valid package. The concept API now edits a deep copy so the state layer can compare the
  actual previous value instead of observing an already-mutated object.
- The deterministic demo now includes the commercial agent boundary. In each real browser,
  the production UI creates a work, continues from one to three chapters, observes automatic
  delivery, edits chapter two's outline, regenerates that chapter, verifies that delivery is
  absent, then explicitly rebuilds and renders the new package. The final matrix passed 24/24
  in Chromium, Firefox and WebKit with no application warning, console/page error or external
  HTTP(S) request. Focused source-invalidation tests cover chapter, concept, world and character
  inputs plus the same-source no-op case.
- Fresh isolated Python 3.11 and 3.14 gates each reported 553 passed, 2 Windows privilege
  skips, 31 subtests and 84% branch coverage; the Node state suite remained 46/46. Both quality
  reports cover all 105 inputs with no changed or skipped check, and the audited 3.11 run found
  no published vulnerability in either Python lock. The formal projects remain at 38/77 files
  with their recorded tree hashes, while the real `.env` remains 599 bytes with its original
  timestamp and SHA-256.
- The current continuation generalized the same transactional validity boundary to every
  optional projection. Concept edits now invalidate short drama, reader, branch, audio and
  delivery results; world, character, outline and prose edits remove only the projection
  families that consume them. Same-source saves retain valid results. The outline API and
  branch-adoption path now edit detached copies, while the state layer still detects legacy
  callers that mutated the loaded outline before saving. Paragraph surgery likewise edits a
  detached chapter so the previous/current comparison cannot be defeated by object aliasing.
- Failure-path evidence preloads an old chapter-two short drama, reader report and branch
  result, changes the outline, rewrites the prose, and deliberately makes the replacement
  short-drama model boundary fail. The committed rewrite contains none of those stale
  projections in memory, on disk or after reload. The isolated browser service also supplies
  deterministic reader/branch results without opening provider transport. Chromium, Firefox
  and WebKit each completed the enriched create/continue/simulate/edit/rewrite/rebuild flow;
  the locked matrix passed 24/24. The intentionally failed optional short-drama stage emitted
  only a correlation ID and did not fail the main task or expose provider content.
- Fresh locked Python 3.11 and 3.14 gates for this projection-validity change each reported
  558 passed, 2 Windows privilege skips, 31 subtests and 84% branch coverage; the Node state
  suite remained 46/46. Both quality reports cover all 105 current inputs with no changed or
  skipped check, and the audited 3.11 run found no published vulnerability in either Python
  lock. No model provider was called and both gates used isolated temporary project roots.
- The next continuation closed the recovery-path gap created by safe invalidation. New
  revision-bound background jobs rebuild the selected chapter's short drama, the current
  concept/cast audio configuration and commercial delivery without rewriting prose. Both
  HTTP admission and the worker's operation lease validate the captured revision before a
  model component is called. Focused concurrency tests prove a queued stale rebuild makes no
  model call. The UI exposes native, labelled rebuild controls and disables them when their
  canonical inputs are absent.
- Chromium testing exposed a misleading render fallback: when chapter two's stale script
  had been removed, the multimodal tab displayed chapter one's script. That cross-chapter
  fallback was removed and a direct JavaScript regression now proves the selected chapter
  renders an explicit empty state. The enriched Chromium/Firefox/WebKit workflow then passed
  24/24: each engine observes automatic short-drama failure after a rewrite, sees no stale or
  cross-chapter script, independently rebuilds the correct chapter script and audio package,
  and finally rebuilds commercial delivery. The Node state suite now contains 48 tests.
- Fresh locked Python 3.11 and 3.14 gates for the independent-rebuild change each reported
  565 passed, 2 Windows privilege skips, 31 subtests and 84% branch coverage; the Node state
  suite passed 48/48. Both reports exactly match all 105 controlled input fingerprints with
  no changed or skipped check, and the audited 3.11 run found no published vulnerability in
  either Python lock. All model boundaries remained deterministic and offline under isolated
  temporary project roots.
- The latest continuation moved reader-retention and narrative-branch simulation onto the
  same revision-bound background contract as the other model-backed projections. Both HTTP
  admission and the worker lease validate the initiating page revision before a component is
  constructed or called; focused concurrency evidence proves a stale queued task makes no
  model call. The browser now tracks the admitted `task_id`, so a deterministic job that
  finishes before the first status poll still refreshes the correct project instead of
  leaving an apparently empty result.
- The production Chromium/Firefox/WebKit flow passed 24/24 after this change and displayed
  the queued chapter-two reader and branch results without changing chapter ownership or
  issuing an external HTTP(S) request. Fresh locked Python 3.11.15 and 3.14.0 gates each
  reported 569 passed, 2 Windows privilege skips, 31 subtests and 84% branch coverage; the
  Node state suite passed 49/49. Both reports exactly match all 105 controlled input hashes,
  with no changed or skipped check; the audited 3.11 run found no published vulnerability in
  either Python lock.
- A further admission audit found that the continuation modal captured project identity but
  omitted its revision. A stale page could therefore request paid planning or prose work
  against a state different from the one the author reviewed. Web continuation now requires
  `expected_revision`, validates it before reserving the worker, and validates it again under
  the worker's project lease before planning recovery or any model call. Direct CLI
  continuation keeps its operation-lease protection and does not need a browser revision.
- The real Chromium/Firefox/WebKit create-and-continue workflow again passed 24/24 with the
  bound continuation request. Fresh locked Python 3.11.15 and 3.14.0 gates each reported
  572 passed, 2 Windows privilege skips, 31 subtests and 84% branch coverage; Node passed
  49/49. Both reports match all 105 controlled inputs, with zero changed/skipped check, and
  the audited 3.11 run found no published vulnerability in either Python lock.
- The product brand is now **Moirai** across the browser title/header, CLI help, FastAPI
  metadata, server startup/recovery copy, documentation, examples, Node package metadata and
  newly exported EPUB identifiers. Persisted `.novelengine_*` folders, logger namespaces and
  temporary-prefix conventions deliberately retain their historical names so existing
  transaction journals, trash entries, revision tombstones and diagnostics remain compatible.
- The branded Chromium/Firefox/WebKit matrix passed 24/24 and directly asserted the Moirai
  document title and top-bar label in every engine. Fresh locked Python 3.11.15 and 3.14.0
  gates each reported 573 passed, 2 Windows privilege skips, 31 subtests and 84% branch
  coverage; Node passed 49/49. Both reports match all 105 controlled inputs with zero
  changed/skipped check, and the audited 3.11 run found no published vulnerability in either
  Python lock.
- A read-only review of the separate `Desktop/Moirai` React prototype retained one product
  idea: searching across prose and structured story data. Its React/Vite migration, browser
  `localStorage` source of truth, mock achievements/tour, decorative dashboard duplication,
  unimplemented shortcut claims and review action that raised scores without applying the
  generated revision were deliberately not imported. The prototype directory was not edited.
- The retained idea was rebuilt on the production architecture as a bounded, read-only,
  revision-returning search endpoint and integrated into the existing command palette. It
  searches lazy chapter bodies, characters, foreshadowing, outline, world and concept data;
  escapes result copy, cancels superseded requests, rejects stale project/revision responses,
  and jumps to the actual chapter or owning view without changing the project revision.
- The final locked Python 3.11.15 and 3.14.0 gates each passed 576 tests, 2 expected Windows
  privilege skips and 31 subtests at 84% branch coverage; the Node state suite passed 52/52,
  including consistent repeated-whitespace normalization between browser and server search.
  Both reports match all 107 controlled inputs with zero changed or skipped checks, and the
  audited 3.11 run found no published vulnerability in either Python lock. The final real
  Chromium/Firefox/WebKit matrix passed 27/27, including a prose hit that navigates between
  chapters while preserving the project revision. One unrelated Firefox compliance-panel
  click timing failure did not reproduce in 19 focused reruns; the subsequent full 27-test
  matrix passed without application console/page errors or external HTTP(S) requests.
