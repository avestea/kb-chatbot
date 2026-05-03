from typing import Callable, Awaitable
from src.lib.errors import UnsupportedMimeTypeError

Parser = Callable[[bytes, str], Awaitable[str]]

_PARSERS: dict[str, Parser] = {}


def get_parser_for(mime_type: str) -> Parser:
    parser = _PARSERS.get(mime_type)
    if parser is None:
        raise UnsupportedMimeTypeError(mime_type)
    return parser


def _register():
    from src.worker.parsers.pdf import parse_pdf
    from src.worker.parsers.docx import parse_docx
    from src.worker.parsers.html import parse_html
    from src.worker.parsers.txt import parse_txt

    _PARSERS["application/pdf"] = parse_pdf
    _PARSERS["application/vnd.openxmlformats-officedocument.wordprocessingml.document"] = parse_docx
    _PARSERS["text/html"] = parse_html
    _PARSERS["text/plain"] = parse_txt


_register()
