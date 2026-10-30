from .config import Settings
from .db import Database
from .router import SudoRouterClient
from .storage import ObjectStore


class Runtime:
    def __init__(self, cfg=None, router_transport=None):
        self.cfg = cfg or Settings()
        self.db = Database(self.cfg.database_url)
        self.pattern_db = (Database(self.cfg.database_url+".pattern.db")
                           if self.cfg.database_url.startswith("sqlite") else self.db)
        self.objects = ObjectStore(self.cfg)
        self.router = SudoRouterClient(self.cfg, router_transport)
        if self.cfg.production:
            self.db.assert_runtime_role()

    def close(self):
        self.router.http.close()
        self.db.engine.dispose()
        if self.pattern_db is not self.db:
            self.pattern_db.engine.dispose()
