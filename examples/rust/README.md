# Example: a Rust project in the sandbox

The sandbox has no network, so cargo must find every crate locally. The plan's `prepare` block vendors the crates pinned in `Cargo.lock` on the host (`cargo vendor` runs no build scripts), into the plan's cache; the sandbox mounts that read-only and [cargo-config.toml](cargo-config.toml) points cargo at it.

**Image**: add the toolchain to [../../sandbox/Containerfile](../../sandbox/Containerfile), e.g. start `FROM docker.io/library/rust:1.99.0-slim-trixie`, `rustup component add rustfmt clippy rust-src` (the standard library source, for offline lookups with `rg`), and `chmod -R a+rX /usr/local/rustup /usr/local/cargo` (Qwen runs as uid 1000).

**Plan** (the relevant parts; `crate` is the crate's folder in the repo, `.` if it is the root):

```json
{
 "prepare": {
  "inputs": ["Cargo.lock"],
  "commands": [
   ["mkdir", "-p", "{cache}/vendor"],
   ["podman", "run", "--rm", "--init", "-v", "{worktree}:/src:ro", "-v", "{cache}/vendor:/vendor",
    "docker.io/library/rust:1.99.0-slim-trixie", "sh", "-c",
    "cd /src && cargo vendor -q --locked --versioned-dirs /vendor >/dev/null"]
  ]
 },
 "sandbox": {
  "image": "localhost/qwen-sandbox-rust",
  "env": {"CARGO_HOME": "/cargo-home", "CARGO_TARGET_DIR": "/target", "CARGO_NET_OFFLINE": "true"},
  "tmpfs": ["/cargo-home"],
  "volumes": ["/target"],
  "mounts": ["{cache}/vendor:/vendor:ro", "{here}/examples/rust/cargo-config.toml:/cargo-home/config.toml:ro"]
 },
 "environment_note": "Cargo works offline against pinned, vendored crates; their sources are under `/vendor` (search them with `rg`).",
 "verify": [
  "cargo fmt --check",
  "cargo clippy --locked --offline -q --all-targets -- -D warnings {lints}",
  "cargo test --locked --offline -q"
 ],
 "vars": {"lints": ""},
 "fail_patterns": ["^test (\\S+) \\.\\.\\. FAILED"],
 "forbidden": ["Cargo.lock", "Cargo.toml", "build.rs"]
}
```

Notes from use:

- **Lints instead of prose.** Denying `unwrap`, `expect`, `panic`, indexing and lossy casts in `[lints]` made Qwen write idiomatic Rust without reminders; prose rules were ignored more often than compiler errors. A step can add lints with `"vars": {"lints": "-W clippy::too_many_lines"}`.
- **`Cargo.lock` protected** (forbidden, or `protected` to pause for a review): a changed lock file means new code from outside.
- The `/target` volume keeps the build between attempts; the verify container gets its own, so Qwen can't plant artefacts in it.
