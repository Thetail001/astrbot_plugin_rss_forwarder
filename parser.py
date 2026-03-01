class FeedParser:
    """解析层：负责将原始内容转换为标准事件对象。"""

    def parse(self, raw_items: list[dict]) -> list[dict]:
        # 预留：按需接入 RSS/Atom 解析逻辑。
        return raw_items
