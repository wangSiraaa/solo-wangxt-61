from __future__ import annotations

import os


class Settings:
    # docker-compose 中通过环境变量覆盖为 PostgreSQL：
    # postgresql+psycopg2://pilot:pilot@db:5432/pilotage
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./pilotage.db")
    # 离船时间（分钟）：上一任务结束后引航员离船所需的固定时间
    DISEMBARK_MINUTES: int = int(os.getenv("DISEMBARK_MINUTES", "10"))
    SOLVER_TIME_LIMIT_SECONDS: float = float(os.getenv("SOLVER_TIME_LIMIT_SECONDS", "10"))
    SOLVER_SEED: int = 42


settings = Settings()
