from bs4 import BeautifulSoup


async def parse_html(data: bytes, filename: str) -> str:
    soup = BeautifulSoup(data, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)
