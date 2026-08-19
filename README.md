# Android Build Broker

A manually operated macOS broker that lets a sandboxed agent request an Android
smoke APK without nesting `cplt`. The broker runs on the host; each accepted
request launches one fresh, top-level cplt sandbox containing the target's dirty
worktree.

## Requirements

- macOS with Python 3, Git, and `cplt` on `PATH`
- A target Git repository with an executable build entrypoint
- A build that writes one fixed repository-relative artifact

No packages, services, LaunchAgents, dotfile changes, `.cplt.toml`, or host
installation are used.

## Architecture And Trust

`broker.py` is the trusted host controller. It owns the single-instance `flock`,
heartbeat, one-second polling, atomic claiming, retention, signals, and the cplt
process group. `worker.py` runs inside a new cplt sandbox and independently
validates the request and source, invokes the configured build argv without a
shell, validates the artifact, and publishes the result. The generated
`scripts/request-android-build.py` client computes the same source identity and
atomically publishes requests from the target repository.

The target repository, requesting agent, build, and their output are hostile
input. Requests use strict schemas, UUID filenames, owned ordinary `0600` files,
size limits, no-follow checks, and atomic replacement. Runtime directories and
configured paths reject symlinks where crossed. Secret-like environment variable
names are removed before launching the build, but arbitrary build output cannot
be guaranteed free of credentials.

Each build also gets an isolated Java `user.home` under cplt's temporary
directory. This prevents Gradle and Maven integrations from reading host files
such as `~/.m2/settings.xml` without granting that file to the sandbox. Existing
cplt-provided `JAVA_TOOL_OPTIONS` are preserved. Native access is enabled for
unnamed build-JVM modules so Android Gradle Plugin can invoke CMake on Java 25;
cplt continues to enforce filesystem, process, and network access.

The cplt sandbox deliberately receives `--allow-localhost-any`, because Gradle
uses random loopback ports. **Target build code can reach any service listening
on localhost while a build runs.** No Maven settings or cache-exec access is
granted.

## Source Identity

Protocol v1 hashes `HEAD` plus all tracked files and all untracked, non-ignored
files returned by NUL-delimited Git plumbing. `.git/`, `.artifacts/`, and
Git-ignored paths are excluded. Records use raw filesystem path bytes,
length-prefixing, file contents, symlink targets, tracked deletions, and canonical
`100644`/`100755` modes. The worker recomputes this before and after the build.
Any disagreement produces `stale` and no published APK.

This detects source changes but is not a filesystem snapshot. A deliberately
racing worktree can still cause the build itself to observe different moments;
the post-build identity prevents such a run from being published as valid.

## Initialize A Repository

Initialization shows proposed changes and asks before writing:

```sh
python3 broker.py init-repo /absolute/path/to/repository
```

It only installs or updates the generated
`scripts/request-android-build.py` and ensures `.gitignore` has a root-equivalent
`.artifacts/` rule. It refuses to overwrite an unknown client. Runtime
directories are not created during initialization.

## Run

`trene` has trusted defaults:

```sh
python3 broker.py serve /Users/kjetil/code/privat/trene
```

Those defaults are:

```text
scripts/build-android-smoke-apk.sh all
artifact: .artifacts/android/trene.apk
Android SDK: ~/Library/Android/sdk
```

Every other repository requires explicit configuration. Repeat `--build-arg`
for each argv element:

```sh
python3 broker.py serve /path/to/repo \
  --build-script scripts/build-apk.sh \
  --build-arg all \
  --artifact .artifacts/android/app.apk \
  --android-sdk ~/Library/Android/sdk
```

Only one broker can run. `.runtime/broker.json` publishes its PID, session ID,
and canonical target path, but `.runtime/broker.lock` and its held `flock` are
authoritative. The foreground process logs concise, timestamped request and build
lifecycle events; idle polling and heartbeat updates are intentionally silent.
The trusted Android SDK path is validated at startup, exposed read-only to cplt,
and exported to the build as `ANDROID_HOME` and `ANDROID_SDK_ROOT`.

## Request And Poll

With the foreground broker running, execute in the target repository:

```sh
python3 scripts/request-android-build.py
```

The client prints JSON containing `requestId` and repository-relative
`statusPath`. Poll that file; no separate wait command exists:

```sh
while true; do cat .artifacts/android/results/REQUEST_ID/status.json; sleep 1; done
```

Runtime layout:

```text
.artifacts/android/
  heartbeat.json
  requests/<request-id>.json
  claimed/<request-id>.json
  results/<request-id>/
    status.json
    build.log
    app-smoke.apk
```

`status.json` moves from `queued` to `building`, then to `passed`, `failed`,
`stale`, or `rejected`. Terminal status is atomically published only after logs
and any APK are complete. A passed status contains `artifactPath`,
`artifactSize`, and `artifactSha256`.

Stable error codes include `busy`, `broker_restarted`, `broker_stopped`,
`invalid_request`, `invalid_request_file`, `request_too_large`,
`request_expired`, `unsupported_protocol`, `validation_failed`,
`source_mismatch`, `source_changed`, `build_timeout`, `build_failed`,
`worker_failed`, `artifact_missing`, `artifact_invalid`, and `artifact_failed`.

There is no queue, cancellation, or rate limit. One request builds; valid
same-session requests found while busy are rejected with `busy`. Requests expire
after 30 minutes, and builds time out after 30 minutes.

## Stop And Retention

Press `Ctrl-C`. During a build the broker terminates the entire cplt process
group and publishes `failed` with `broker_stopped`. The heartbeat is removed and
closing the lock descriptor releases exclusivity.

Terminal result directories are retained for seven days and capped at the 20
newest. Cleanup runs at startup and after each build. Active and non-terminal
results are never retention candidates.

## Tests

```sh
python3 -m unittest discover -v
```

Tests use temporary Git repositories and local fake build processes; they do not
need Android tooling.

## Troubleshooting

- `Another Android build broker is already running`: stop the foreground broker;
  do not rely on or delete PID metadata while its `flock` is held.
- `broker heartbeat is stale`: start the broker and retry; requests are tied to
  one broker session and never silently wait for a later run.
- `source_mismatch` or `source_changed`: stop modifying non-ignored source during
  request/build and submit a new request.
- `Build script must be an executable ordinary file`: set the executable bit in
  the target worktree and ensure no configured path component is a symlink.
- `build_timeout` or `build_failed`: inspect the complete per-request `build.log`.
- Dependency/network failures must be fixed in the target build or cplt policy;
  do not grant `~/.m2/settings.xml` or other host access without a separate
  security decision.
