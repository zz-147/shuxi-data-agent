"""运行配置：环境变量优先，显式参数覆盖，默认值兜底."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv 不是硬依赖，缺失时仍可用 --mock 演示
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - 可选依赖
    load_dotenv = None  # type: ignore[assignment]


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"


def _root_dir() -> Path:
    # src/shuxi/config.py -> 仓库根目录
    return Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


@dataclass(frozen=True)
class Settings:
    """全局配置。frozen 保证运行期不可变，便于测试与审计."""

    provider: str = "mock"
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    root_dir: Path = field(default_factory=_root_dir)
    db_path: Path = field(default_factory=lambda: _root_dir() / "data" / "demo.db")
    log_dir: Path = field(default_factory=lambda: _root_dir() / "logs")
    artifacts_dir: Path = field(default_factory=lambda: _root_dir() / "artifacts")
    max_steps: int = 8
    query_row_limit: int = 200
    auto_approve: bool = False
    temperature: float = 0.2
    request_timeout: float = 60.0

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        """从 .env / 环境变量构造配置，overrides 优先级最高."""
        if load_dotenv is not None:
            load_dotenv(_root_dir() / ".env", override=False)

        values = {
            "provider": _env("SHUXI_PROVIDER", "mock"),
            "model": _env("SHUXI_MODEL", DEFAULT_MODEL),
            "api_key": _env("DASHSCOPE_API_KEY", ""),
            "base_url": _env("SHUXI_BASE_URL", DEFAULT_BASE_URL),
            "db_path": Path(_env("SHUXI_DB_PATH", str(_root_dir() / "data" / "demo.db"))),
            "log_dir": Path(_env("SHUXI_LOG_DIR", str(_root_dir() / "logs"))),
            "artifacts_dir": Path(_env("SHUXI_ARTIFACTS_DIR", str(_root_dir() / "artifacts"))),
            "max_steps": int(_env("SHUXI_MAX_STEPS", "8")),
            "query_row_limit": int(_env("SHUXI_QUERY_ROW_LIMIT", "200")),
            "auto_approve": _env("SHUXI_AUTO_APPROVE", "false").lower() in {"1", "true", "yes"},
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def ensure_dirs(self) -> "Settings":
        """创建运行期输出目录（幂等）."""
        for directory in (self.log_dir, self.log_dir / "traces", self.artifacts_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def is_mock(self) -> bool:
        return self.provider.lower() == "mock"
