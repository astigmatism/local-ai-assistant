# Test Results

Validated in the build environment with:

```bash
cd <repo-root>
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src pytest -q -p pytest_asyncio.plugin
```

Result:

```text
61 passed
```

Coverage in this update includes production wake config validation for `Rosalina`, automatic migration away from the old packaged default phrase, simulated wake diagnostics preservation, custom PocketSphinx dictionary generation for `rosalina`, overlapping `arecord` rolling-buffer windows decoded by `pocketsphinx_continuous -infile /dev/stdin`, diagnostic summary statistics, external wake stdout parsing, external wake subprocess termination, stderr surfacing for subprocess failures, pause/resume behavior, wake health failures for missing commands/models/runtime, migration from persisted simulated wake to the packaged production engine, acknowledgement-before-capture ordering including failed and long wake acknowledgements, local command gating only after wake acknowledgement and prompt capture, and the LLM no-model router request contract.
