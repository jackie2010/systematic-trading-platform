# -*- coding: utf-8 -*-
"""
Phase 3: providers removed from register_schema. Providers come from Recorder registration.
"""
import logging
from typing import List

import sqlalchemy
from sqlalchemy import MetaData
from sqlalchemy.ext.declarative import DeclarativeMeta
from sqlalchemy.sql.ddl import CreateTable
from sqlalchemy.sql.expression import text

from zvt.contract import zvt_context
from zvt.contract.schema import TradableEntity, Mixin
from zvt.utils.utils import add_to_map_list

logger = logging.getLogger(__name__)


def ensure_schema_tables_and_indexes(engine, schema_base: DeclarativeMeta, db_name: str):
    """Create tables and indexes for schema. Used by lazy init."""
    schema_base.metadata.create_all(bind=engine)
    for table_name, table in iter(schema_base.metadata.tables.items()):
        db_meta = MetaData()
        db_meta.reflect(bind=engine)
        db_table = db_meta.tables[table_name]
        existing_columns = [c.name for c in db_table.columns]
        added_columns = [c for c in table.columns if c.name not in existing_columns]
        index_list = []
        with engine.connect() as con:
            if db_name in ("zvt_info", "stock_news", "stock_tags", "stock_quote"):
                con.execute(text("PRAGMA journal_mode=WAL;"))
                con.execute(text("PRAGMA journal_size_limit=1073741824;"))
            else:
                con.execute(text("PRAGMA journal_mode=DELETE;"))
            rs = con.execute(text("PRAGMA INDEX_LIST('{}')".format(table_name)))
            for row in rs:
                index_list.append(row[1])
            try:
                if added_columns:
                    ddl_c = engine.dialect.ddl_compiler(engine.dialect, CreateTable(table))
                    for added_column in added_columns:
                        stmt = text(
                            f"ALTER TABLE {table_name} ADD COLUMN {ddl_c.get_column_specification(added_column)}"
                        )
                        logger.info(f"{engine.url} migrations:\n {stmt}")
                        con.execute(stmt)
                for col in [
                    "timestamp", "entity_id", "code", "report_period",
                    "created_timestamp", "updated_timestamp",
                ]:
                    if col in table.c:
                        column = getattr(table.c, col)
                        index_name = "{}_{}_index".format(table_name, col)
                        if index_name not in index_list:
                            sqlalchemy.schema.Index(index_name, column).create(engine)
                for cols in [("timestamp", "entity_id"), ("timestamp", "code")]:
                    if cols[0] in table.c and cols[1] in table.c:
                        column0 = getattr(table.c, cols[0])
                        column1 = getattr(table.c, cols[1])
                        index_name = "{}_{}_{}_index".format(table_name, cols[0], cols[1])
                        if index_name not in index_list:
                            sqlalchemy.schema.Index(index_name, column0, column1).create(engine)
            except Exception as e:
                logger.error(e)


def register_entity(entity_type: str = None):
    """
    function for register entity type

    :param entity_type:
    :type entity_type:
    :return:
    :rtype:
    """

    def register(cls):
        # register the entity
        if issubclass(cls, TradableEntity):
            entity_type_ = entity_type
            if not entity_type:
                entity_type_ = cls.__name__.lower()

            if entity_type_ not in zvt_context.tradable_entity_types:
                zvt_context.tradable_entity_types.append(entity_type_)
                zvt_context.tradable_entity_schemas.append(cls)
            zvt_context.tradable_schema_map[entity_type_] = cls
        return cls

    return register


def register_schema(
    db_name: str,
    schema_base: DeclarativeMeta,
    entity_type: str = None,
    internal: bool = False,
):
    """
    Register schema. Providers come from Recorder registration (provider_map_recorder).
    Tables/engines are created lazily on first get_db_engine(provider, db_name).
    For schemas without Recorders, add to config: storage.schema_providers.

    :param internal: If True, schema is business/internal data, not tied to external data source.
                     Internal schemas only care about storage routing, do not participate in
                     provider switching (e.g. zvt_info, trader_info, stock_tags).
    """
    schemas = []
    for item in schema_base.registry.mappers:
        cls = item.class_
        if type(cls) == DeclarativeMeta:
            if zvt_context.dbname_map_schemas.get(db_name):
                schemas = zvt_context.dbname_map_schemas[db_name]
            zvt_context.schemas.append(cls)
            if issubclass(cls, Mixin):
                cls._zvt_db_name = db_name
                cls._zvt_internal = internal  # internal=True: business data, no external data source
            if entity_type:
                add_to_map_list(the_map=zvt_context.entity_map_schemas, key=entity_type, value=cls)
            schemas.append(cls)

    zvt_context.dbname_map_schemas[db_name] = schemas
    zvt_context.dbname_map_base[db_name] = schema_base
    if internal:
        zvt_context.internal_db_names.add(db_name)


# the __all__ is generated
__all__ = ["register_entity", "register_schema", "ensure_schema_tables_and_indexes"]
