import pdfplumber
import io


async def parse_pdf(data: bytes, filename: str) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = []
        for page in pdf.pages:
            # x_tolerance=2 catches inter-word gaps (~2.7pt) that the default of 3 misses,
            # preventing words from being concatenated into one token.
            words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
            if words:
                lines: dict[float, list[str]] = {}
                for w in words:
                    key = round(w["top"])
                    lines.setdefault(key, []).append(w["text"])
                text = "\n".join(" ".join(lines[k]) for k in sorted(lines))
                pages.append(text)
    return "\n\n".join(pages)
