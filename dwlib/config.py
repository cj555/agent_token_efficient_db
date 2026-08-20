"""仓库配置与路径解析。所有模块的路径都必须经过这里，不得硬编码。"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_MARKER = "warehouse.yaml"


def find_repo_root(start: Path | None = None) -> Path:
    """向上查找 warehouse.yaml 所在目录。支持 DW_REPO 环境变量覆盖。"""
    env = os.environ.get("DW_REPO")
    if env:
        return Path(env).resolve()
    cur = (start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / _MARKER).is_file():
            return cand
    raise FileNotFoundError(
        f"未找到 {_MARKER}：请在数据仓库目录内运行 dw，或设置 DW_REPO 环境变量。"
    )


@lru_cache(maxsize=8)
def load_config(root: Path | None = None) -> dict[str, Any]:
    r = root or find_repo_root()
    with (r / _MARKER).open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Paths:
    """集中式路径解析。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or find_repo_root()).resolve()
        self.cfg = load_config(self.root)
        s = self.cfg.get("storage", {})
        raw_root = Path(s.get("root", "storage"))
        self.storage = raw_root if raw_root.is_absolute() else self.root / raw_root
        self._raw = s.get("raw", "raw")
        self._blob = s.get("blob", "blob")
        self._curated = s.get("curated", "curated")
        self._tmp = s.get("tmp", "tmp")

    # --- 代码与合约 ---
    @property
    def datasets(self) -> Path:
        return self.root / "datasets"

    @property
    def contracts_dir(self) -> Path:
        return self.root / "data_contracts"

    @property
    def index_md(self) -> Path:
        return self.contracts_dir / "INDEX.md"

    @property
    def graph_json(self) -> Path:
        return self.contracts_dir / "graph.json"

    @property
    def registry_json(self) -> Path:
        return self.contracts_dir / "registry.json"

    @property
    def external_yaml(self) -> Path:
        return self.contracts_dir / "external_sources.yaml"

    @property
    def health_dir(self) -> Path:
        return self.root / ".health"

    @property
    def plans_dir(self) -> Path:
        return self.root / ".dw" / "plans"

    def dataset_dir(self, name: str) -> Path:
        return self.datasets / name

    def contract_file(self, name: str) -> Path:
        return self.dataset_dir(name) / "contract.yaml"

    # --- 数据层 ---
    def raw(self, source_id: str) -> Path:
        return self.storage / self._raw / source_id

    def blob(self, source_id: str) -> Path:
        return self.storage / self._blob / source_id

    def curated(self, dataset: str) -> Path:
        return self.storage / self._curated / dataset

    def tmp(self, dataset: str) -> Path:
        return self.storage / self._tmp / dataset

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.resolve().as_posix()

    def validate_name(self, name: str) -> None:
        pat = self.cfg.get("naming", {}).get("pattern", "^[a-z][a-z0-9_]*$")
        if not re.match(pat, name):
            raise ValueError(f"dataset 名 '{name}' 不符合命名规范 {pat}（建议 <family>__<table>）")


def paths(root: Path | None = None) -> Paths:
    return Paths(root)
