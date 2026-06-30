# Test Results

Validated in the build environment with:

```bash
cd /mnt/data/repo
PYTHONPATH=src pytest -q
```

Result:

```text
37 passed
```

Coverage added in this update includes production wake config validation, simulated wake diagnostics preservation, external wake stdout parsing, external wake subprocess termination and pause/resume behavior, wake health failures for missing commands/models/runtime, migration from persisted simulated wake to the packaged production engine, local command gating only after wake and prompt capture, and the LLM no-model router request contract.
