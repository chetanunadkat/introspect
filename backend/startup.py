import os
from contextlib import asynccontextmanager

from db_models import Base, Users
from fastapi import FastAPI
from sqlalchemy import Engine, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.automap import automap_base
from utils_logging import LOGGER

################################################################################
# This file is used to consolidate all the startup and shutdown events into a
# single function. We package them all within a single lifespan function so that
# we can ensure that all the startup events are completed successfully before
# the server starts accepting requests, and that all the shutdown events are
# completed successfully before the server shuts down.
################################################################################


async def init_db(engine: AsyncEngine):
    """
    Initialize database tables and update existing tables if their structure has changed.
    Args:
        engine: AsyncEngine for main database
    """
    try:
        from sqlalchemy import inspect
        
        async with engine.begin() as conn:
            # need to create the vector extension first before creating tables
            # that contain vector columns
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            
            # Create a SQLAlchemy inspector to examine the database
            inspector = await conn.run_sync(lambda sync_conn: inspect(sync_conn))
            
            # Get list of existing tables in the database
            existing_tables = set(await conn.run_sync(lambda sync_conn: inspector.get_table_names()))
            
            # Create tables that don't exist
            await conn.run_sync(Base.metadata.create_all)
            
            # For existing tables, check and update their structure
            for table_name, table in Base.metadata.tables.items():
                if table_name in existing_tables:
                    LOGGER.info(f"Checking table structure for {table_name}")
                    
                    # Get existing columns in the table using the inspector
                    existing_columns = {
                        col['name']: col for col in 
                        await conn.run_sync(lambda sync_conn: inspector.get_columns(table_name))
                    }
                    
                    # Compare with the model columns and add missing ones
                    for column in table.columns:
                        if column.name not in existing_columns:
                            # For complex types like Enum, Vector, etc., we need to ensure they exist
                            if hasattr(column.type, '__visit_name__') and column.type.__visit_name__ == 'ENUM':
                                # For Enum types, check if the enum type exists and create it if not
                                enum_type_name = column.type.name
                                enum_values = [repr(val.value) for val in column.type.enums]
                                
                                # Check if enum type exists
                                enum_exists = await conn.execute(text(
                                    f"SELECT 1 FROM pg_type WHERE typname = '{enum_type_name}'"
                                ))
                                
                                if not enum_exists.scalar():
                                    # Create the enum type
                                    LOGGER.info(f"Creating enum type {enum_type_name}")
                                    await conn.execute(text(
                                        f"CREATE TYPE {enum_type_name} AS ENUM ({', '.join(enum_values)})"
                                    ))
                            
                            # Handle the column creation with proper SQL
                            column_type = column.type.compile(dialect=engine.dialect)
                            nullable = "NULL" if column.nullable else "NOT NULL"
                            
                            # Handle default values appropriately
                            default = ""
                            if column.default is not None:
                                if hasattr(column.default, 'arg'):
                                    if callable(column.default.arg):
                                        # For callable defaults like datetime.now, we can't use them directly in SQL
                                        if 'datetime.now' in str(column.default.arg):
                                            default = "DEFAULT CURRENT_TIMESTAMP"
                                        else:
                                            # Skip default for other callables, they'll be handled by SQLAlchemy
                                            pass
                                    else:
                                        # Special handling for enum types - can't use DEFAULT with enum references in ALTER TABLE
                                        if column.name == 'user_type':
                                            # We'll update this value after adding the column
                                            LOGGER.info(f"Will update column {column.name} with default value after adding it")
                                            default = ""
                                        elif column.name == 'status':
                                            # We'll update this value after adding the column
                                            LOGGER.info(f"Will update column {column.name} with default value after adding it")
                                            default = ""
                                        # For literal values, quote strings but not numbers or booleans
                                        elif isinstance(column.default.arg, (str,)):
                                            default = f"DEFAULT '{column.default.arg}'"
                                        else:
                                            default = f"DEFAULT {column.default.arg}"
                            
                            try:
                                LOGGER.info(f"Adding missing column {column.name} to {table_name}")
                                await conn.execute(text(
                                    f"ALTER TABLE {table_name} ADD COLUMN {column.name} {column_type} {nullable} {default};"
                                ))
                                
                                # Update the enum columns with default values after adding them
                                if column.name == 'user_type' and table_name == 'users':
                                    LOGGER.info("Setting default value for user_type column")
                                    await conn.execute(text(
                                        "UPDATE users SET user_type = 'ADMIN' WHERE user_type IS NULL;"
                                    ))
                                elif column.name == 'status' and table_name == 'users':
                                    LOGGER.info("Setting default value for status column")
                                    await conn.execute(text(
                                        "UPDATE users SET status = 'ACTIVE' WHERE status IS NULL;"
                                    ))
                            except Exception as column_err:
                                LOGGER.error(f"Error adding column {column.name} to {table_name}: {str(column_err)}")
                                # Continue with other columns even if one fails
                    
                    # Identify columns in the database that are not in the model (removed columns)
                    # We don't automatically drop columns as it could lead to data loss
                    model_column_names = {column.name for column in table.columns}
                    extra_columns = set(existing_columns.keys()) - model_column_names
                    
                    if extra_columns:
                        LOGGER.warning(f"Table {table_name} has extra columns in the database that are not in the model: {extra_columns}")
                        LOGGER.warning("These columns will not be automatically dropped to prevent data loss")
    except Exception as e:
        LOGGER.error(f"Error initializing database: {str(e)}")
        raise


async def create_admin_user():
    """
    Create admin user if it doesn't exist or update existing admin user's type and status.
    This ensures the admin account is properly configured with the new fields.
    """
    from db_config import engine
    from datetime import datetime
    from db_models import UserType, UserStatus

    # auth_utils imported inside here to prevent a race condition because of multiple calls to get_db_engine
    from auth_utils import get_hashed_password, login_user, validate_user
    from sqlalchemy import select, update

    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
    hashed_password = get_hashed_password(admin_username, admin_password)

    try:
        # Check if admin user exists
        async with engine.begin() as conn:
            user_result = await conn.execute(
                select(Users).where(Users.username == admin_username)
            )
            user = user_result.fetchone()
            
            if user:
                # User exists, ensure it has the correct user_type and status
                LOGGER.info(f"Admin user '{admin_username}' already exists, updating configuration")
                await conn.execute(
                    update(Users)
                    .where(Users.username == admin_username)
                    .values(
                        user_type=UserType.ADMIN,
                        status=UserStatus.ACTIVE
                    )
                )
            else:
                # Create new admin user
                LOGGER.info(f"Creating admin user '{admin_username}'")
                await conn.execute(
                    insert(Users).values(
                        username=admin_username,
                        hashed_password=hashed_password,
                        token=hashed_password,
                        user_type=UserType.ADMIN,
                        status=UserStatus.ACTIVE,
                        created_at=datetime.now()
                    )
                )
                LOGGER.warning("Default admin user created with default password. Please change it immediately!")
    except Exception as e:
        LOGGER.error(f"Error creating/updating admin user: {str(e)}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables and admin user on startup"""
    try:
        # Run validation on current llm sdk version to ensure models used are supported
        from db_config import engine

        LOGGER.info("Running startup events...")

        # Initialize database tables
        await init_db(engine)
        
        # Create admin user if doesn't exist
        await create_admin_user()
        
        LOGGER.info("All startup events completed successfully")

        LOGGER.info("🚀 You can now visit the app in the browser at http://localhost:80")
        LOGGER.info("The default username is `admin` and default password is also `admin`")

        yield

        LOGGER.info("Shutting down...")
    except Exception as e:
        LOGGER.error(f"Startup failed: {str(e)}")
        raise
