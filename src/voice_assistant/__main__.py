from __future__ import annotations

import uvicorn

from .config import load_config_store_from_env


def main() -> None:
    store = load_config_store_from_env()
    cfg = store.get_active()
    uvicorn.run("voice_assistant.app:app", host=cfg.admin.host, port=cfg.admin.port, reload=False)


if __name__ == "__main__":
    main()
