async def parse_txt(data: bytes, filename: str) -> str:
    return data.decode("utf-8", errors="replace")
