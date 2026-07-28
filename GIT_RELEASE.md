# Git Release Guide — AI Engine v1.0.1

Documentation only — **no Git commands were executed as part of this task.** This is a reference for whoever performs the actual tagging once a Git repository is initialized for this project (note: as of this release, the working directory has no `.git` repository yet — see `AI_ENGINE_VERSION.md`'s configuration snapshot).

## Recommended Commands

```
git tag ai-engine-v1.0.1
```
**When to use**: after the release branch/commit containing all v1.0.1 changes (Grounding fix, Citation Attribution fix, Production Validation Center, documentation) has been reviewed and merged to the main branch. This creates a permanent, addressable marker for exactly this commit — the same commit validated by `9fbf0d50-756c-4434-8fe1-5bda45db41e1`.

```
git push origin ai-engine-v1.0.1
```
**When to use**: immediately after creating the tag locally, to publish it to the shared remote so the release is visible to the whole team and available for deployment tooling/CI to reference. Only run this once the tag is confirmed correct — a pushed tag is effectively public and should not be casually re-tagged.

```
git checkout ai-engine-v1.0.1
```
**When to use**: when you need to inspect or redeploy the EXACT code state of this release — e.g. reproducing a production incident, or rolling back a bad deployment to the last known-good baseline. This detaches HEAD to the tagged commit; create a new branch from here if you intend to make changes.

```
git show ai-engine-v1.0.1
```
**When to use**: to quickly inspect what the tag points to (commit message, author, diff summary) without checking it out — useful for release audits or confirming the tag was created against the intended commit before pushing.

## Notes

- These commands are recommendations only; they were **not executed** as part of this task.
- Tag naming follows `ai-engine-vX.Y.Z` to distinguish AI Engine releases from any other component's versioning in this repository.
- Once a `.git` repository exists for this project, the git commit hash should also be recorded in `baseline_v1.0.1.json`'s `configuration_snapshot.application.git_commit` field (currently "not tracked").
