# Test Results

Validated in the build environment with:

```bash
cd <repo-root>
PYTHONPATH=src pytest -q
```

Result:

```text
45 passed
```

Coverage in this update includes production wake config validation for `Rosalina`, automatic migration away from the old packaged default phrase, simulated wake diagnostics preservation, custom PocketSphinx dictionary generation for `rosalina`, overlapping `arecord` rolling-buffer windows decoded by `pocketsphinx_continuous -infile /dev/stdin`, diagnostic summary statistics, external wake stdout parsing, external wake subprocess termination, stderr surfacing for subprocess failures, pause/resume behavior, wake health failures for missing commands/models/runtime, migration from persisted simulated wake to the packaged production engine, local command gating only after wake and prompt capture, and the LLM no-model router request contract.
