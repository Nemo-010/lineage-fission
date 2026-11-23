# lineage-fission

Split one LineageOS build across many GitHub Actions jobs, then put it back
together. One repository, one workflow, one matrix (GitHub caps a matrix at
**256 jobs per run**).

    plan   manifests -> project set -> sections      (offline, seconds)
    split  out/combined-$DEVICE.ninja -> shard targets (offline, seconds)
    shard  ninja <my targets> -> tar of my outputs   (matrix, N jobs)
    merge  hash-verified union of all shards         (one job)
    freshen mtimes + real .ninja_log -> ninja accepts (one job)
    finish  images, OTA zip, signed artefacts        (one job)
    verify  sha256sum -c on the masternode           (20 GB is plenty)

## Try it in one command

Stdlib only, Python 3.9+, no `pip install`. The manifest half runs offline
against the real LineageOS 22.1 manifest set kept in `fixtures/`:

    python3 -m fission.cli plan \
      --manifest fixtures/lineage-22.1-default.xml \
      --local-manifest fixtures/muppets-lineage-22.1.xml \
      --manifest-url https://github.com/LineageOS/android \
      --include-dir fixtures --device alioth --vendor xiaomi \
      --out /tmp/plan.json

That is 1719 upstream projects, 1474 after pruning the other-device vendor
trees, in about **0.07 s**.

    python3 -m unittest discover -s test      # 13 tests

The end-to-end test needs `ninja` on PATH (`apt install ninja-build`, or
`pip install ninja`); it splits a generated Android-shaped graph three ways,
builds the shards with real ninja, merges them, and diffs the result against a
single-workspace build file for file. Without ninja it skips itself rather than
pretending to pass.


## Does this architecture make sense?

Mostly yes, with three corrections. The measurements say so:

| Claim | Measured | Source |
|---|---|---|
| A workflow matrix can run 256 jobs | 256 | GitHub Actions limits doc |
| Concurrent jobs (Free/Pro/Team) | 20 / 40 / 60 | GitHub Actions limits doc |
| Single job wall-clock cap | 6 h | GitHub Actions limits doc |
| Hosted runner | 4 vCPU, 16 GB RAM, **14 GB SSD** | GitHub-hosted runners doc |
| Actions artifact storage (Free/Pro/Team/Ent) | **500 MB / 1 GB / 2 GB / 50 GB total** | GitHub Actions limits doc |
| Actions cache, per repo | 10 GB | GitHub Actions limits doc |
| Release assets | <2 GiB per file, 1000 files, **no total cap** | GitHub releases doc |
| LineageOS 22.1 manifest | 1719 projects (1319 default + 155 `snippets/lineage.xml` + 245 muppets) | `fission plan` on the real manifest set |
| alioth project set | 1474 after pruning 246 other-device vendor trees | `fission plan --device alioth --vendor xiaomi` |
| `frameworks/base` packed | **8.1 GB** | GitHub API |
| `frameworks/base` blobless shallow clone | **3.1 MB, 1.7 s** | `git clone --depth 1 --filter=blob:none` |
| `frameworks/base/core` sparse checkout | 267 MB, 32 s | `git sparse-checkout set core` |
| `frameworks/base` full checkout | did **not** finish in 4m41s, 935 MB fetched, ended in a promisor error | same tree |
| Finished OTA for alioth | **1.443 GB** | download.lineageos.org API |
| Split/merge correctness | split build == single-workspace build, file-for-file | `test/test_e2e.py` with real ninja |
| Work duplicated by splitting | 1.67x @2, 2.25x @4, 2.69x @8, 3.01x @16 shards | `tools/bench_split.py` (weights synthetic) |

### Correction 1: the masternode must not assemble

Assembly needs the whole installed tree plus image packaging, and the finished
zip alone is 1.44 GB. With 20 GB you can hold the *result* comfortably and
cannot hold the *workspace*. So the final `brunch` runs on a runner that has
room; the masternode downloads `lineage-*.zip` + `SHA256SUMS` and runs
`sha256sum -c`. That is a 30-second job even on slow WSL.

### Correction 2: artifacts cannot carry the build

500 MB–2 GB total artifact storage versus gigabytes of module outputs. Shard
outputs go to **release assets** (2 GiB per file, 1000 files, no total cap) or
to any bucket. Artifacts are fine only for the small stuff (plan, matrix,
local manifests).

### Correction 3: shards must be sparse, and the bootstrap cannot be avoided

14 GB is smaller than `frameworks/base` packed (8.1 GB), let alone 1474
projects. Each shard therefore syncs only its own projects
(`repo sync <paths...>`, one generated local manifest per shard). But something
still has to generate `out/combined-$DEVICE.ninja` from the full tree first —
that is the bootstrap job, and no amount of matrix parallelism removes it.

## What is verified, and how

`python3 -m unittest discover -s test` — 13 tests. The important one is
`test_split_build_merge_matches_reference`, which:

1. generates an Android-shaped ninja project (45 compiles, 14 archives,
   14 installs, 2 image edges, cross-section links),
2. builds it once as a reference,
3. splits it 3 ways, runs **real ninja** in three separate workspaces,
4. packs each worker's owned outputs, merges them into a fresh workspace,
5. freshens, then builds the *remaining* goal and compares the whole `out/`
   tree against the reference, file by file.

Three bugs were found by that test and are now covered by regression tests:

* **tar stores mtime as whole seconds.** Merged outputs came back ~0.6 s older
  than their inputs, so ninja declared everything dirty. The manifest carries
  nanoseconds and merge restores from it (`test_mtime_survives_to_the_nanosecond`).
* **ninja trusts the log's mtime, not the file's.** Freshening files alone did
  nothing; the `.ninja_log` entries had to be rewritten too.
* **a missing log entry means "rebuild"**, and an unknown log version is
  silently discarded. The log is now part of the artifact contract: each shard
  ships its own real entries, and merge writes them out with the version header
  the workers actually used (v7 for ninja 1.13).

Also verified against the real world: the manifest parser on the actual
LineageOS 22.1 manifest set, including `<include name="snippets/lineage.xml">`,
`remove-project`, `extend-project`, group semantics, and
`lineage.dependencies` expansion.

## What is *not* verified

Be blunt about these before trusting a real run:

* **No real LineageOS tree was built here.** There is no `combined.ninja` in
  this workspace, so the graph shape, per-section disk usage, and real build
  times are unmeasured. The 7.4k-edge benchmark uses synthetic weights.
* **`.ninja_deps` is not transported yet.** Rules with `deps = gcc` consult the
  deps log; if the assembler does not have it, ninja will want to re-run
  compiles. Reasoned, not measured — it is the next thing to check on a real
  tree.
* **Project-level sharding is too coarse for 256 shards.** Balance degrades to
  1.68 at 16 shards because `frameworks/base` is one project. Splitting inside
  a project needs module-level attribution (`out/soong/.intermediates/<module>/`
  encodes the module name), which is not implemented.
* **`repo sync <paths...>` leaves a subset tree.** Whether ninja is happy with
  a tree that has the shard's files but not every `Android.bp` is untested;
  shards do not re-run soong, so it may well work — but "may well" is not a
  measurement.
* **The workflow has not been executed.** Its structure is checked (valid YAML,
  job graph, matrix expression); the steps are not.

## Usage

    # 1. manifests -> plan
    python3 -m fission.cli plan \
      --manifest fixtures/lineage-22.1-default.xml \
      --local-manifest fixtures/muppets-lineage-22.1.xml \
      --manifest-url https://github.com/LineageOS/android \
      --include-dir fixtures \
      --device alioth --vendor xiaomi --expand-deps http \
      --out plan.json

    # 2. graph -> shards (needs a configured build tree)
    python3 -m fission.cli split --ninja out/combined-alioth.ninja --root . \
      --plan plan.json --workers 8 --ninja-log out/.ninja_log --out split/

    # 3. shards -> matrix + one local manifest each
    python3 -m fission.cli matrix --plan plan.json --split-plan split/plan.json --out mx/

    # 4. per shard (in CI): build your targets, then
    python3 -m fission.cli pack --root . --targets mx/targets.w0.txt \
      --tar w0.tar --worker w0

    # 5. assembler
    python3 -m fission.cli merge --dest . --strict --ownership-dir split/ --artifact w0.tar ...
    python3 -m fission.cli freshen --ninja out/combined-alioth.ninja --root .
    # ... then brunch/soong builds only what is left

`python3 -m fission.cli doctor --root .` says whether a machine can hold a
build, in measured numbers, before anything long starts.

Running the tests needs ninja on PATH (`apt install ninja-build`, or
`pip install ninja` into a venv); without it the end-to-end test skips itself
rather than pretending to pass:

    NINJA=/path/to/ninja python3 -m unittest discover -s test -v

## Layout

    fission/manifest.py   repo manifest semantics, includes, groups, lineage.dependencies
    fission/sections.py   path -> section policy (and a loud round-trip)
    fission/ninja.py      ninja graph reader, closures, .ninja_log read/rewrite
    fission/partition.py  edge attribution, sharding, balance/duplication report
    fission/artifacts.py  pack/merge, hash verification, ownership, log transport
    fission/freshen.py    mtime reconciliation for merged trees
    fission/cli.py        plan | split | matrix | pack | merge | freshen | verify | doctor
    test/                 13 tests, including the real-ninja end-to-end
    tools/bench_split.py  scaling experiment on AOSP-shaped graphs
    .github/workflows/fission.yml  one repo, one matrix, 5 jobs
