# -*- coding: utf-8 -*-
"""
Storage abstraction for ZVT.
Phase 1: StorageBackend interface + SqliteStorageBackend with existing path logic.
Phase 2: path_template and base_path from config.
"""
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


def _default_data_path() -> str:
    """Get data path from zvt_env, with fallback for tests."""
    try:
        from zvt import zvt_env
        return zvt_env.get("data_path", ".")
    except Exception:
        return "."


def _get_storage_config() -> dict:
    """Load storage config from zvt_config. Lazy import to avoid cycles."""
    try:
        from zvt import zvt_config
        return zvt_config.get("storage") or {}
    except Exception:
        return {}


class StorageBackend(ABC):
    """
    Abstract storage backend. Decouples physical storage from domain/read/record logic.
    """

    @abstractmethod
    def get_engine(self, storage_id: str, data_path: Optional[str] = None) -> Engine:
        """
        Get or create SQLAlchemy engine for the given storage_id.
        :param storage_id: Logical storage identifier (e.g. "em_stock_meta").
        :param data_path: Base path for data files. If None, uses zvt_env["data_path"].
        :return: Engine instance.
        """
        raise NotImplementedError

    @abstractmethod
    def get_session_factory(self, storage_id: str, data_path: Optional[str] = None):
        """
        Get or create sessionmaker for the given storage_id.
        Must be configured with engine before use (caller's responsibility, e.g. in register_schema).
        :param storage_id: Logical storage identifier.
        :param data_path: Base path for data files.
        :return: sessionmaker instance.
        """
        raise NotImplementedError


class SqliteStorageBackend(StorageBackend):
    """
    SQLite storage backend. Default path: {data_path}/{provider}/{provider}_{db_name}.db
    Config (zvt_config["storage"]):
      - base_path: override data path
      - path_template: format string with {base_path}, {provider}, {db_name}, {storage_id}
    """

    def __init__(self, path_template: Optional[str] = None, base_path: Optional[str] = None):
        """
        :param path_template: Optional. Overrides config. Placeholders: {base_path}, {provider}, {db_name}, {storage_id}
        :param base_path: Optional. Overrides config.
        """
        self._path_template_override = path_template
        self._base_path_override = base_path
        self._engine_map = {}
        self._session_factory_map = {}

    def _get_path_template(self) -> Optional[str]:
        """Path template: constructor override > config > None (use default)."""
        if self._path_template_override is not None:
            return self._path_template_override
        config = _get_storage_config()
        return config.get("path_template")

    def _get_base_path(self, data_path: Optional[str]) -> str:
        """Base path: param > constructor override > config > zvt_env."""
        if data_path is not None:
            return data_path
        if self._base_path_override is not None:
            return self._base_path_override
        config = _get_storage_config()
        cfg_path = config.get("base_path")
        if cfg_path is not None:
            return cfg_path
        return _default_data_path()

    def _storage_id_to_path(self, storage_id: str, data_path: str) -> str:
        """
        Compute SQLite file path from storage_id.
        storage_id format: "{provider}_{db_name}" (e.g. "em_stock_meta").
        Default path: {data_path}/{provider}/{provider}_{db_name}.db
        """
        if "_" not in storage_id:
            raise ValueError(f"Invalid storage_id: {storage_id}, expected '{{provider}}_{{db_name}}'")
        provider, db_name = storage_id.split("_", 1)
        path_template = self._get_path_template()
        if path_template:
            return path_template.format(
                base_path=data_path, provider=provider, db_name=db_name, storage_id=storage_id
            )
        provider_dir = os.path.join(data_path, provider)
        if not os.path.exists(provider_dir):
            os.makedirs(provider_dir)
        return os.path.join(provider_dir, f"{provider}_{db_name}.db?check_same_thread=False")

    def get_engine(self, storage_id: str, data_path: Optional[str] = None) -> Engine:
        data_path = self._get_base_path(data_path)
        if storage_id in self._engine_map:
            return self._engine_map[storage_id]
        db_path = self._storage_id_to_path(storage_id, data_path)
        url = "sqlite:///" + db_path
        engine = create_engine(
            url, echo=False, json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False)
        )
        self._engine_map[storage_id] = engine
        return engine

    def get_session_factory(self, storage_id: str, data_path: Optional[str] = None):
        data_path = self._get_base_path(data_path)
        if storage_id in self._session_factory_map:
            return self._session_factory_map[storage_id]
        fac = sessionmaker()
        self._session_factory_map[storage_id] = fac
        return fac

# Default instance; can be replaced for testing or custom backends
_default_storage_backend: Optional[StorageBackend] = None


def get_storage_backend() -> StorageBackend:
    """Get the default storage backend. Creates SqliteStorageBackend on first use."""
    global _default_storage_backend
    if _default_storage_backend is None:
        _default_storage_backend = SqliteStorageBackend()
    return _default_storage_backend


def set_storage_backend(backend: StorageBackend):
    """Replace default storage backend. Useful for tests."""
    global _default_storage_backend
    _default_storage_backend = backend


__all__ = [
    "StorageBackend",
    "SqliteStorageBackend",
    "get_storage_backend",
    "set_storage_backend",
]
