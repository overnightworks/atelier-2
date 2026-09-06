# `grok agent stdio` (ACP) behaviour probes

Seven one-prompt captures of `grok agent stdio` (Agent Client Protocol, pinned
binary `grok-1.0.5`) against a private, throwaway `~/.grok` credential copy
and a throwaway scratch git repository — never the operator's live checkout
or live `~/.grok`. Each `<nn>-<fact>.jsonl` file is the full newline-delimited
JSON-RPC transcript in both directions, one JSON object per line, with a
relative timestamp (`t`, seconds since process start) and a final
`meta-exit` line naming the child's exit code and whether it had to be force
killed. This is exploratory measurement for issue #1177 (the runner↔agent
port design); nothing here is production code.

1. **Shell-command permission, and `--allow Bash`.** Asking the model to run
   a shell command does produce a `session/request_permission` — its
   `toolCall.kind` is `"execute"`, `rawInput.variant` is `"Bash"`, and its
   `_meta` names it `run_terminal_command`. Adding `--allow Bash` before
   `agent stdio` does **not** suppress it: both runs in
   `01-run-terminal-permission.jsonl` (run `0` = no flag, run `1` = `--allow
   Bash`, distinguished by the `run` field on every record) hit the identical
   permission request. In stdio/ACP mode the client's own answer is what
   governs, not the CLI's own allow/deny rule set.
2. **Client answers `reject_once`.** The turn ends immediately, it does not
   continue trying an alternative: the tool call's `session/update` moves
   straight to `status: "failed"` ("User rejected the execution for tool
   `run_terminal_command`"), and the `session/prompt` response resolves with
   `stopReason: "cancelled"` right after. See `02-reject-once.jsonl`.
3. **Client errors `fs/write_text_file`.** The agent surfaces the failure on
   the same tool call (`tool_call_update` → `status: "failed"`, content
   `"Error: failed to write README.md: IO Error: RuntimeError: ..."`), then
   tries a different path to the same goal — here, a `run_terminal_command`
   (`printf >> README.md`) — and finishes the turn normally
   (`stopReason: "end_turn"`). It does not abort the turn on one failed
   client RPC. See `03-write-text-file-error.jsonl` (this run also used
   `--deny Bash` to force the first attempt through the edit tool, which is
   what actually calls `fs/write_text_file`; the bash retry it made anyway is
   further evidence for fact 6 below — `--deny Bash` did not stop it).
4. **`session/cancel` mid-turn.** `stopReason: "cancelled"`; the child exits
   on its own (`exit_code: 0`, `forced_kill: false`) within the 8-second
   grace window after stdin closes — no kill signal was needed. See
   `04-session-cancel-mid-turn.jsonl`. Caveat: cancel was sent 3 seconds after
   the prompt, once `run_terminal_command` was already `in_progress`
   (`sleep 5`); this run's permission request for that tool call had not yet
   arrived when cancel fired, so it does not additionally show a
   cancel-during-a-pending-permission-request interleaving.
5. **`--max-turns 1` before `agent stdio`.** Does **not** cut the turn at 1.
   `05-max-turns-1.jsonl` used the append-and-reply prompt with `--deny Bash`
   (forcing the 3-turn edit-tool path also seen in the un-flagged baseline
   capture) and still finished all 3 turns with `stopReason: "end_turn"`;
   `_meta.usage.numTurns` reports `3`. `--max-turns` is a top-level flag but
   does not bind inside the persistent ACP stdio session — only headless
   one-shot invocations were verified against it before this probe.
6. **Do `--tools`/`--deny` still bind in stdio mode?** No — measured for
   `--deny`, and consistent with fact 1's `--allow` result. `06-deny-write-path.jsonl`
   ran with `--deny "Write(denied.txt)"` and asked the model to write exactly
   that denied path: the agent proposed the write, the CLI still emitted a
   normal `session/request_permission` for it (`kind: "edit"`, `rawInput.variant:
   "Write"`), the client (auto-)approved it, and the file was written — then
   the agent redundantly wrote the same content again via
   `run_terminal_command`, which the client also had to approve via its own
   `session/request_permission`. The deny rule never caused an outright
   refusal or a different tool-call outcome; the client's permission answer
   was the only thing enforced. `--tools` was not separately probed (same
   client-side gate applies to every builtin tool call regardless of name, so
   a second capture would not add information over `01`/`06`).
7. **`--no-leader` footprint (storage-mode).** `--storage-mode` does not
   exist in this CLI version (`grok --help`, `grok agent --help`, `grok agent
   stdio --help` all checked) — that half of the question cannot be measured.
   `07-no-leader-storage-footprint.jsonl` ran `grok agent --no-leader stdio`
   with a tool-free prompt (no shell/edit calls, to isolate the CLI's own
   footprint): it still wrote `config.toml`, `models_cache.json`,
   `active_sessions.json`(+`.lock`), `agent_id`, `.metadata_version`,
   `.config-init.lock`, `logs/unified.jsonl`, `managed_config.lock`, the
   packaged `docs/user-guide/*.md`, and a whole `sessions/<url-encoded-cwd>/`
   directory (chat history, events, rewind points, summary, system prompt)
   into the private `HOME` — well beyond the one credential-copy file.
   `--no-leader` does not change that; it only controls whether this process
   is the shared leader.

## Capture list

| File | Scenario |
| --- | --- |
| `01-run-terminal-permission.jsonl` | fact 1, two runs (no flag / `--allow Bash`) |
| `02-reject-once.jsonl` | fact 2 |
| `03-write-text-file-error.jsonl` | fact 3 |
| `04-session-cancel-mid-turn.jsonl` | fact 4 |
| `05-max-turns-1.jsonl` | fact 5 |
| `06-deny-write-path.jsonl` | fact 6 |
| `07-no-leader-storage-footprint.jsonl` | fact 7 |

Every fixture was scanned for the private credential copy's exact token,
refresh-token, email, and OIDC client id values (never printed) before commit;
none were present. The recording machine's hostname, the operator's user
name, and its recorded home directory path and session scratch paths under
`/tmp/claude-1000/…` were scrubbed too, each replaced everywhere by the same
placeholder: `operator-host` for the hostname, `operator` for the user name,
`/home/operator` for the home directory prefix, and
`/tmp/operator-session/…` for the session scratch prefix.
