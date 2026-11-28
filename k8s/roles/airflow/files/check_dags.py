"""Parse the configured DAG folder the way the dag-processor does, and fail on any import error.

Usage: python check_dags.py

Runs at image build time, inside the Airflow image, with `AIRFLOW__CORE__DAGS_FOLDER` set, so
a DAG that raises on import, or a folder that yields no DAG, fails the build instead of
reaching a tag. Exits 1 and prints each failing file's error.
"""

import argparse
import sys


def main() -> int:
    from airflow.configuration import conf
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=conf.get("core", "dags_folder"))
    for path, error in sorted(bag.import_errors.items()):
        print(f"{path}: {error}", file=sys.stderr)
    if not bag.dag_ids:
        print("no DAG parsed", file=sys.stderr)
        return 1
    print(f"{len(bag.dag_ids)} DAGs parsed, {len(bag.import_errors)} import errors")
    return 1 if bag.import_errors else 0


if __name__ == "__main__":
    argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0]).parse_args()
    sys.exit(main())
