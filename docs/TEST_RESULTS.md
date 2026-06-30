# Test Results

Validated in the build environment with:

```bash
cd <repo-root>
PYTHONPATH=src pytest -q
```

Result:

```text
40 passed
```

Coverage added in this update includes production wake config validation, simulated wake diagnostics preservation, the `arecord` -> `pocketsphinx_continuous -infile /dev/stdin` production wake wrapper, external wake stdout parsing, external wake subprocess termination, stderr surfacing for subprocess failures, pause/resume behavior, wake health failures for missing commands/models/runtime, migration from persisted simulated wake to the packaged production engine, local command gating only after wake and prompt capture, and the LLM no-model router request contract.
