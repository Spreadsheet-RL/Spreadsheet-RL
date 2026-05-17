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

import asyncio
from typing import Generic, Optional, TypeVar

import structlog

logger = structlog.stdlib.get_logger()

T = TypeVar('T')


# FIXME: type inferenced to Any
class Singleton(Generic[T]):
    _instance: Optional[T] = None
    _lock: Optional[asyncio.Lock] = None

    @classmethod
    async def get_instance_async(cls, *args, **kwargs):
        if not cls._instance:
            if not cls._lock:
                cls._lock = asyncio.Lock()
            async with cls._lock:
                if not cls._instance:
                    self = cls(*args, **kwargs)
                    assert hasattr(self, 'async_init'), 'async singletons must define async_init function'
                    await self.async_init()
                    cls._instance = self
                    logger.debug('singleton class initialized', name=cls.__name__)
        return cls._instance

    @classmethod
    def get_instance_sync(cls, *args, **kwargs):
        if not cls._instance:
            self = cls(*args, **kwargs)
            assert not hasattr(
                self, 'async_init'), f'class {cls.__name__} has async_init function, init it with get_instance_async.'
            cls._instance = self
            logger.debug('singleton class initialized', name=cls.__name__)
        return cls._instance
