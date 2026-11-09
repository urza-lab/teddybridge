import os

import uvicorn

from . import config

if __name__ == "__main__":
    try:
        cfg = config.load()
    except config.MissingConfig:
        if not os.environ.get("TB_CONFIG_FILE"):
            raise
        # Noch nicht eingerichtet: Einrichtung im Browser statt Abbruch (app/setup.py)
        host, _, port = os.environ.get("LISTEN", "0.0.0.0:8085").rpartition(":")
        uvicorn.run("app.setup:app", host=host or "0.0.0.0", port=int(port or 8085), log_level="info",
                    server_header=False)
    else:
        uvicorn.run("app.main:app", host=cfg.host, port=cfg.port, log_level="info",
                    # Nur vom eingetragenen Proxy (Caddy) X-Forwarded-For/-Proto übernehmen
                    proxy_headers=bool(cfg.trusted_proxies), forwarded_allow_ips=",".join(cfg.trusted_proxies) or None,
                    server_header=False)
