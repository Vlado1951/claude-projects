class Database:
    def init_schema(self) -> None:
        raise NotImplementedError

    def mark_seen(self, post_id: str) -> None:
        raise NotImplementedError

    def is_seen(self, post_id: str) -> bool:
        raise NotImplementedError

    def record_classification(self, **kwargs: object) -> None:
        raise NotImplementedError

    def record_alert(self, **kwargs: object) -> None:
        raise NotImplementedError

    def last_24h_summary(self) -> dict[str, int]:
        raise NotImplementedError
