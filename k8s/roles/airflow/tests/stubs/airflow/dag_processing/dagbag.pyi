class DagBag:
    import_errors: dict[str, str]
    dag_ids: list[str]
    def __init__(self, dag_folder: str) -> None: ...
