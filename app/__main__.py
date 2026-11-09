import uvicorn

from . import config

if __name__ == "__main__":
    cfg = config.load()
    uvicorn.run("app.main:app", host=cfg.host, port=cfg.port, log_level="info",
                # Nur vom eingetragenen Proxy (Caddy) X-Forwarded-For/-Proto übernehmen
                proxy_headers=bool(cfg.trusted_proxies), forwarded_allow_ips=",".join(cfg.trusted_proxies) or None,
                server_header=False)
