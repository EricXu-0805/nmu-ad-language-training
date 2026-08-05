from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlmodel import SQLModel

from alembic import context

from app import models  # noqa: F401 —— 注册全部表进 metadata
from app.db import DEFAULT_URL

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# URL 与应用同源：显式 Config URL 优先（测试/运维定向迁移），否则与
# app.db.make_engine 一样先读 DATABASE_URL，最后才落到本地 SQLite。
# 若此处忽略环境变量，serve.sh 会升级 data/app.db，而应用却连到未迁移的部署库。
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", os.environ.get("DATABASE_URL") or DEFAULT_URL)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
# disable_existing_loggers=False: the default (True) silently disables any
# logger a caller already created (e.g. app.tts at test-collection time),
# breaking caplog assertions in tests that run after an in-process upgrade.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = SQLModel.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
