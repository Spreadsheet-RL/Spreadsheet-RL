# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import sys
from types import NoneType
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

import structlog
from databases import Database
from fastapi import HTTPException

from sandbox.configs.run_config import RunConfig
from sandbox.utils.execution import max_concurrency

if TYPE_CHECKING:
    from sandbox.datasets.types import GetPromptByIdRequest, GetPromptsRequest

logger = structlog.stdlib.get_logger('db')
config = RunConfig.get_instance_sync()

__database_datalake = None
__database_sqlite = None
__cached_tables = {}


def get_table_name(name, db_type: Literal['datalake', 'sqlite']):
    return f'"{name}"' if db_type == 'sqlite' else name


async def load_samples_to_sqlite(table_name, samples, db):
    global __cached_tables
    type_map = {int: 'INT', str: 'TEXT', NoneType: 'TEXT'}
    for sample in samples:
        for key, value in sample.items():
            if type(value) not in type_map:
                sample[key] = json.dumps(value)
    columns = ", ".join([f"{key} {type_map[type(value)]}" for key, value in samples[0].items()])
    logger.info(f'sqlite in-memory table {table_name}: {columns}')
    create_table_query = f'CREATE TABLE "{table_name}" ({columns})'
    await db.execute(query=create_table_query)

    cols = ', '.join([f':{key}' for key in samples[0].keys()])
    query = f'INSERT INTO "{table_name}" VALUES ({cols})'
    await db.execute_many(query=query, values=samples)
    __cached_tables[table_name] = list(samples[0].keys())
    logger.info(f'{len(samples)} loaded into {table_name}')


#  convert sample jsons into in-memory sqlite db for local testing
async def jsonls_to_tables(jsonl_directory, database):
    for file_name in os.listdir(jsonl_directory):
        if not file_name.endswith('.jsonl'):
            continue
        table_name = os.path.splitext(file_name)[0]

        samples = []
        with open(os.path.join(jsonl_directory, file_name), "r") as jsonl_file:
            samples = [json.loads(line) for line in jsonl_file.readlines()]
        await load_samples_to_sqlite(table_name, samples, database)


async def load_cache(datalake, sqlite, tables):
    try:
        for table in tables:
            table_name = table.name
            columns = table.columns
            query = f'SELECT {", ".join(columns)} FROM {get_table_name(table_name, "datalake")} ORDER BY id'
            raw_rows = await datalake.fetch_all(query)
            rows = [{k: row[idx] for idx, k in enumerate(row)} for row in raw_rows]
            await load_samples_to_sqlite(table_name, rows, sqlite)
    except Exception as e:
        logger.error(f'failed to load cache to memory: {e}')
        sys.exit(1)


async def get_datalake_database() -> Database:
    user = config.dataset.database.backend.user
    password = config.dataset.database.backend.password
    host = config.dataset.database.backend.host
    port = config.dataset.database.backend.port

    db = Database(f'mysql+aiomysql://{user}:{password}@[{host}]:{port}')
    await db.connect()
    logger.info(f'datalake connected')
    return db


async def get_sqlite_database() -> Database:
    if config.dataset.database.cache.path == 'memory':
        db = Database('sqlite+aiosqlite:///file::memory:?cache=shared', uri=True)
    else:
        db_path = os.path.abspath(os.path.join(__file__, '../../', config.dataset.database.cache.path))
        db = Database(f'sqlite+aiosqlite://{db_path}', uri=True)
    await db.connect()
    logger.info(f'sqlite connected')
    return db


@max_concurrency(1)
async def get_databases():
    global __database_datalake, __database_sqlite
    if __database_datalake is None and config.dataset.database.backend.type == 'mysql':
        __database_datalake = await get_datalake_database()
    if __database_sqlite is None and config.dataset.database.cache is not None:
        __database_sqlite = await get_sqlite_database()
        for source in config.dataset.database.cache.sources:
            if source.type == 'local':
                samples_path = os.path.abspath(os.path.join(__file__, '../../', source.path))
                await jsonls_to_tables(samples_path, __database_sqlite)
            if source.type == 'mysql':
                await load_cache(__database_datalake, __database_sqlite, source.tables)
    return __database_datalake, __database_sqlite


def should_use_sqlite(table_name: str, columns: Optional[List[str]] = None) -> bool:
    if table_name not in __cached_tables:
        return False
    cached_columns = __cached_tables[table_name]
    if columns is not None and set(cached_columns) < set(columns):
        logger.warning(f'not all columns cached for table {table_name}: {cached_columns} < {columns}')
        return False
    return True


async def get_rows_in_table(request: 'GetPromptsRequest',
                            table_name: str,
                            columns: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if request.config.provided_data:
        if not isinstance(request.config.provided_data, list):
            raise HTTPException(status_code=400,
                                detail=f'request.config.provided_data should be a list of str -> str | int')
        if columns:
            return [{k: row[k] for k in columns} for row in request.config.provided_data]
        return request.config.provided_data
    datalake, sqlite = await get_databases()
    col_query = ', '.join(columns) if columns else '*'
    if should_use_sqlite(table_name, columns):
        db = sqlite
        query = f'SELECT {col_query} FROM {get_table_name(table_name, "sqlite")} ORDER BY id LIMIT {request.offset}, {request.limit}'
    else:
        db = datalake
        query = f'SELECT {col_query} FROM {get_table_name(table_name, "datalake")} ORDER BY id LIMIT {request.offset}, {request.limit}'
    try:
        rows = await db.fetch_all(query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'failed to request database with query {query}: {e}')
    return [{k: row[idx] for idx, k in enumerate(row)} for row in rows]


async def get_row_by_id_in_table(request: 'GetPromptByIdRequest',
                                 table_name: str,
                                 columns: Optional[List[str]] = None) -> Dict[str, Any]:
    if request.config.provided_data:
        if not isinstance(request.config.provided_data, dict):
            raise HTTPException(status_code=400,
                                detail=f'request.config.provided_data should be a dict with str -> str | int')
        if columns:
            return {k: request.config.provided_data[k] for k in columns}
        return request.config.provided_data
    datalake, sqlite = await get_databases()
    col_query = ', '.join(columns) if columns else '*'
    id = request.id
    id_query = str(id) if type(id) == int else f"'{id}'"
    if should_use_sqlite(table_name, columns):
        db = sqlite
        query = f'SELECT {col_query} FROM {get_table_name(table_name, "sqlite")} WHERE id = {id_query}'
    else:
        db = datalake
        query = f'SELECT {col_query} FROM {get_table_name(table_name, "datalake")} WHERE id = {id_query}'
    try:
        row = await db.fetch_one(query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'failed to request database with query {query}: {e}')
    return {k: row[idx] for idx, k in enumerate(row)}
