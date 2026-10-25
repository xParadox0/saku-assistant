"""Build synthetic PDFs that copy the *layout* of a BCA e-statement.

No real statement is ever committed. Column positions, the multi-line description
layout, the summary block and the encryption (AES-256 with an empty user password,
which is how BCA ships its e-statements) were measured from a real file with every
character reduced to its shape. All names, numbers and amounts here are invented.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from decimal import Decimal

PAGE_H = 842
FONT = 8
# Helvetica advance widths (1/1000 em) for the characters an amount can contain.
_W = {**{d: 556 for d in "0123456789"}, ",": 278, ".": 278, "-": 333}

# Column anchors measured from a real statement (pdfplumber x0 / x1 in points).
X_DATE = 44
X_DESC = 89
X_DESC2 = 194          # second description column on the first row and on continuation lines
X_CBG = 308
X_MUTASI_RIGHT = 437   # mutation amounts are right-aligned here
X_DB_MARK = 442
X_SALDO_RIGHT = 569
HEADER = [(33, "TANGGAL"), (164, "KETERANGAN"), (308, "CBG"), (380, "MUTASI"), (501, "SALDO")]


def fmt(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _width(text: str) -> float:
    return sum(_W.get(ch, 556) for ch in text) * FONT / 1000


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


@dataclass
class Row:
    day: int
    first: str                 # text in the KETERANGAN column, e.g. "TRSF E-BANKING CR"
    amount: Decimal            # positive; direction comes from `credit`
    credit: bool
    detail: list[str] = field(default_factory=list)  # lines in the second description column
    cbg: str | None = None
    show_saldo: bool = True    # BCA prints the running balance only on the last row of a day


@dataclass
class Statement:
    month: int = 9
    year: int = 2026
    account: str = "0123456789"
    opening: Decimal = Decimal("250000.00")
    rows: list[Row] = field(default_factory=list)
    rows_per_page: int = 8
    title: str = "REKENING TAHAPAN"
    currency: str = "IDR"
    # test hooks for corrupted statements
    tamper_saldo_row: int | None = None
    tamper_closing: Decimal | None = None
    tamper_cr_count: int | None = None
    tamper_db_total: Decimal | None = None
    omit_summary: bool = False
    period_text: str | None = None          # overrides "SEPTEMBER 2026" in the header
    show_currency: bool = True
    stray_db_on_continuation: int | None = None   # row index whose first continuation line gets a "DB" marker
    rows_after_summary: bool = False


MONTHS = ["JANUARI", "FEBRUARI", "MARET", "APRIL", "MEI", "JUNI", "JULI", "AGUSTUS",
          "SEPTEMBER", "OKTOBER", "NOVEMBER", "DESEMBER"]


def _page_header(ops: list, st: Statement, page: int, pages: int) -> None:
    ops.append((202, 34, st.title))
    ops.append((30, 64, "KCP CONTOH"))
    ops.append((31, 82, "NAMA CONTOH SAJA"))
    ops.append((324, 81, "NO. REKENING"))
    ops.append((417, 81, ":"))
    ops.append((435, 81, st.account))
    ops.append((324, 97, "HALAMAN"))
    ops.append((417, 97, ":"))
    ops.append((435, 97, f"{page} /{pages}"))
    ops.append((324, 114, "PERIODE"))
    ops.append((417, 114, ":"))
    ops.append((435, 114, st.period_text or f"{MONTHS[st.month - 1]} {st.year}"))
    if st.show_currency:
        ops.append((324, 130, "MATA UANG"))
        ops.append((417, 130, ":"))
        ops.append((435, 130, st.currency))
    ops.append((31, 175, "Catatan contoh yang tidak berisi data apa pun."))
    for x, word in HEADER:
        ops.append((x, 240, word))


def _right(ops: list, right: float, top: float, text: str) -> None:
    ops.append((right - _width(text), top, text))


def _layout(st: Statement) -> list[list[tuple[float, float, str]]]:
    chunks = [st.rows[i:i + st.rows_per_page] for i in range(0, len(st.rows), st.rows_per_page)] or [[]]
    pages = len(chunks)
    out = []
    balance = st.opening
    cr = [r for r in st.rows if r.credit]
    db = [r for r in st.rows if not r.credit]
    index = 0
    for p, chunk in enumerate(chunks, start=1):
        ops: list = []
        _page_header(ops, st, p, pages)
        top = 261
        if p == 1:
            ops.append((X_DATE, top, f"01/{st.month:02d}"))
            ops.append((X_DESC, top, "SALDO AWAL"))
            _right(ops, X_SALDO_RIGHT, top, fmt(st.opening))
            top += 14
        for r in chunk:
            balance += r.amount if r.credit else -r.amount
            ops.append((X_DATE, top, f"{r.day:02d}/{st.month:02d}"))
            ops.append((X_DESC, top, r.first))
            lines = list(r.detail) or [""]
            if lines[0]:
                ops.append((X_DESC2, top, lines[0]))
            if r.cbg:
                ops.append((X_CBG, top, r.cbg))
            _right(ops, X_MUTASI_RIGHT, top, fmt(r.amount))
            if not r.credit:
                ops.append((X_DB_MARK, top, "DB"))
            if r.show_saldo:
                shown = balance + Decimal("1000") if st.tamper_saldo_row == index else balance
                _right(ops, X_SALDO_RIGHT, top, fmt(shown))
            for k, extra in enumerate(lines[1:]):
                top += 12
                if isinstance(extra, tuple):      # (left column text, right column text)
                    left, right = extra
                    ops.append((X_DESC, top, left))
                    if right:
                        ops.append((X_DESC2, top, right))
                else:
                    ops.append((X_DESC2, top, extra))
                if k == 0 and st.stray_db_on_continuation == index:
                    ops.append((X_DB_MARK, top, "DB"))
            top += 14
            index += 1
        if p < pages:
            ops.append((400, 802, "Bersambung ke halaman berikut"))
        elif not st.omit_summary:
            closing = st.tamper_closing if st.tamper_closing is not None else balance
            top += 30
            summary = [
                ("SALDO AWAL", st.opening, None),
                ("MUTASI CR", sum((r.amount for r in cr), Decimal(0)),
                 st.tamper_cr_count if st.tamper_cr_count is not None else len(cr)),
                ("MUTASI DB", st.tamper_db_total if st.tamper_db_total is not None
                 else sum((r.amount for r in db), Decimal(0)), len(db)),
                ("SALDO AKHIR", closing, None),
            ]
            for label, value, count in summary:
                ops.append((208, top, label))
                ops.append((265, top, ":"))
                _right(ops, 368, top, fmt(value))
                if count is not None:
                    ops.append((412, top, str(count)))
                top += 12
            if st.rows_after_summary:
                top += 14
                ops.append((X_DATE, top, f"28/{st.month:02d}"))
                ops.append((X_DESC, top, "TRSF E-BANKING CR"))
                _right(ops, X_MUTASI_RIGHT, top, fmt(Decimal("10.00")))
        out.append(ops)
    return out


def _pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3
    first_page_id = 4
    kids = " ".join(f"{first_page_id + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, ops in enumerate(pages):
        content_id = first_page_id + 2 * i + 1
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 {PAGE_H}] "
                     f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>").encode())
        # pdfplumber reports `top` as the glyph box top; Helvetica ascent at 8pt is ~5.7pt
        stream = "".join(f"BT /F1 {FONT} Tf {x:.2f} {PAGE_H - top - 5.7:.2f} Td ({_esc(t)}) Tj ET\n"
                         for x, top, t in ops).encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
    buf = io.BytesIO()
    buf.write(b"%PDF-1.7\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(buf.tell())
        buf.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = buf.tell()
    buf.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return buf.getvalue()


def _encrypt(data: bytes, user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=user_password, owner_password="synthetic-owner", algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build(st: Statement, *, encrypt: bool = True, user_password: str = "") -> bytes:
    """Return PDF bytes. By default encrypted like BCA ships it: AES-256, empty user password."""
    raw = _pdf(_layout(st))
    return _encrypt(raw, user_password) if encrypt else raw


def bomb(n_ops: int) -> bytes:
    """A tiny, valid one-page PDF whose compressed content stream expands into n_ops text operations.

    Used to prove a crafted upload cannot pin the server's CPU or memory.
    """
    import zlib

    ops: list = []
    _page_header(ops, Statement(), 1, 1)
    head = "".join(f"BT /F1 8 Tf {x:.2f} {PAGE_H - t - 5.7:.2f} Td ({_esc(s)}) Tj ET\n" for x, t, s in ops)
    body = "BT /F1 1 Tf 100 300 Td " + "(x) Tj " * n_ops + "ET\n"
    comp = zlib.compress((head + body).encode("latin-1"), 9)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 {PAGE_H}] "
            "/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>".encode(),
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream"]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.7\n")
    offsets = []
    for num, body_bytes in enumerate(objs, start=1):
        offsets.append(buf.tell())
        buf.write(f"{num} 0 obj\n".encode() + body_bytes + b"\nendobj\n")
    xref = buf.tell()
    buf.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return buf.getvalue()


def corrupt_stream(data: bytes) -> bytes:
    """Break the first text operator of an *unencrypted* fixture so the PDF libraries emit warnings."""
    return data.replace(b"BT /F1 8 Tf", b"BT /F1 8 Tf [[[[ <<", 1)


def sample() -> Statement:
    """A two-page month with every row shape seen in the real layout."""
    return Statement(rows=[
        Row(2, "TRSF E-BANKING CR", Decimal("1500000.00"), True,
            ["0209/FTSCY/WS95031", "1500000.00", "BUDI CONTOH SAJA"]),
        Row(2, "TRSF E-BANKING DB", Decimal("1500000.00"), False,
            ["0209/FTSCY/WS95031", "1500000.00", "pindah dana", "ANI CONTOH SAJA"], show_saldo=False),
        Row(5, "BIAYA ADM", Decimal("15000.00"), False, [], cbg="0998"),
        Row(7, "DB DEBIT", Decimal("45500.00"), False,
            ["TOKO*CONTOH QRC", "1234567890123456/Q"]),
        Row(12, "TRSF E-BANKING CR", Decimal("75000.00"), True,
            ["1209/FTSCY/WS95031", ("TANGGAL :12/09", "75000.00"), "BUDI CONTOH SAJA"]),
        Row(15, "TARIKAN ATM", Decimal("100000.00"), False, ["15/09", "6019001234567890"]),
        Row(20, "TRSF E-BANKING CR", Decimal("200000.00"), True,
            ["2009/FTSCY/WS95031", "200000.00", "GoPay Bank Contoh", "ID1234567890123456", "CONTOH NAMA SAJA"]),
        Row(25, "DB DEBIT", Decimal("60000.00"), False, ["TOKO CONTOH DEBIT POS 123", "MDR 12-34567"]),
        Row(30, "BUNGA", Decimal("312.50"), True, []),
        Row(30, "PAJAK BUNGA", Decimal("62.50"), False, []),
    ])
