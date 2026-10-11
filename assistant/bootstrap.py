"""Create the schema and load everything the assistant needs.

Usage: python -m assistant.bootstrap [--reset-config]
"""
import argparse

from assistant import db
from assistant.config import reset_config, seed_config
from assistant.ingest import main as ingest_main
from assistant.prompts import seed_prompts
from assistant.store_seed import seed_store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset-config", action="store_true",
                        help="overwrite app_config with defaults (undoes injected faults)")
    args = parser.parse_args()

    with db.connect() as conn:
        db.init_schema(conn)
        reset_config(conn) if args.reset_config else seed_config(conn)
        seed_prompts(conn)
        seed_store(conn)
        print("schema, config, prompts and store data ready")
    ingest_main()


if __name__ == "__main__":
    main()
