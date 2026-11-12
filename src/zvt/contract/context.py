# -*- coding: utf-8 -*-
"""
ZVT global context. Phase 1: storage_backend and route_registry are optional
injections; api uses get_storage_backend()/get_route_registry() when not set.
"""
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from zvt.contract.storage import StorageBackend
    from zvt.contract.route_registry import RouteRegistry


class Registry(object):
    """
    Class storing zvt registering meta
    """

    def __init__(self) -> None:
        #: all registered providers
        self.providers = []

        #: all registered entity types(str)
        self.tradable_entity_types = []

        #: all entity schemas
        self.tradable_entity_schemas = []

        #: all registered schemas
        self.schemas = []

        #: tradable entity  type -> schema
        self.tradable_schema_map = {}

        #: global sessions
        self.sessions = {}

        #: provider_dbname -> engine (deprecated: engines now in StorageBackend)
        self.db_engine_map = {}

        #: provider_dbname -> session factory (deprecated: now in StorageBackend)
        self.db_session_map = {}

        #: provider -> [db_name1,db_name2...]
        self.provider_map_dbnames = {}

        #: db_name -> [declarative_base1,declarative_base2...]
        self.dbname_map_base = {}

        #: db_name -> [declarative_meta1,declarative_meta2...]
        self.dbname_map_schemas = {}

        #: entity_type -> related schemas
        self.entity_map_schemas = {}

        #: factor class registry
        self.factor_cls_registry = {}

        #: db_names of internal schemas (business logic data, not tied to external data source)
        self.internal_db_names = set()

        #: Optional StorageBackend; if set, api uses it instead of get_storage_backend()
        self.storage_backend: Optional["StorageBackend"] = None

        #: Optional RouteRegistry; if set, api uses it instead of get_route_registry()
        self.route_registry: Optional["RouteRegistry"] = None


#: :class:`~.zvt.contract.context.Registry` instance
zvt_context = Registry()


# the __all__ is generated
__all__ = ["Registry"]
