"""配置加载：.env 覆盖 + conf/app_config.yaml

注意：DSN 这类含 `?` 与 `=` 的值不走 OmegaConf 插值（会被当成语法错误），
统一由本模块从环境变量读出来拼装。
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from omegaconf import DictConfig, OmegaConf

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


def _build_dsn(cfg: DictConfig) -> str:
    if os.getenv("DW_DSN"):
        return os.environ["DW_DSN"]
    d = cfg.dw
    return (
        f"mysql+asyncmy://{d.user}:{d.password}@{d.host}:{d.port}/{d.database}"
        f"?charset={d.charset}"
    )


def load_config() -> DictConfig:
    cfg = OmegaConf.load(ROOT / "conf" / "app_config.yaml")
    cfg.llm.api_key = os.getenv("LLM_API_KEY", cfg.llm.api_key)
    cfg.llm.base_url = os.getenv("LLM_BASE_URL", cfg.llm.base_url)
    cfg.llm.model_name = os.getenv("LLM_MODEL", cfg.llm.model_name)
    cfg.ontology.resolve_mode = os.getenv("RESOLVE_MODE", cfg.ontology.resolve_mode)
    cfg.dw.dsn = _build_dsn(cfg)
    return cfg


app_config: DictConfig = load_config()
