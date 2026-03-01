class FeedStorage:
    """存储层：负责去重、游标和持久化。"""

    def __init__(self) -> None:
        self._seen_ids: set[str] = set()

    async def has_seen(self, item_id: str) -> bool:
        return item_id in self._seen_ids

    async def mark_seen(self, item_id: str) -> None:
        self._seen_ids.add(item_id)
