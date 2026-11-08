"""Small dependency-free branded PDF renderer for approved quotations."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import unicodedata

from app.models import Quotation
from app.templating import format_money


def _pdf_text(value: object) -> str:
    text = str(value or "").replace("₹", "INR ").replace("—", "-").replace("–", "-").replace("•", "*")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(value: object, width: int = 82) -> list[str]:
    output: list[str] = []
    for paragraph in str(value or "").splitlines() or [""]:
        words = paragraph.split()
        if not words:
            output.append("")
            continue
        line = ""
        for word in words:
            if line and len(line) + len(word) + 1 > width:
                output.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        output.append(line)
    return output


def _date_label(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "Not specified"


def _quote_lines(quote: Quotation) -> list[tuple[str, bool]]:
    lines: list[tuple[str, bool]] = []

    def section(label: str) -> None:
        lines.append((label.upper(), True))

    def item(label: str, value: object) -> None:
        for index, line in enumerate(_wrap(f"{label}: {value}")):
            lines.append((line, False))

    section("Traveller")
    item("Name", quote.customer_name_snapshot)
    lines.append(("", False))

    section("Journey")
    item("Package", f"{quote.package_code_snapshot or ''} {quote.package_title_snapshot}".strip())
    item("Travel dates", f"{_date_label(quote.travel_start_date)} to {_date_label(quote.travel_end_date)}")
    item("Travellers", f"{quote.adults} adults, {quote.children} children, {quote.infants} infants")
    item("Accommodation", quote.accommodation)
    lines.append(("", False))

    section("Pricing")
    item("Basis", quote.price_basis.replace("_", " "))
    item("Adult per-person rate", format_money(quote.adult_unit_price, quote.currency))
    if quote.children:
        item("Child per-person fare", format_money(quote.child_unit_price, quote.currency) if quote.child_unit_price is not None else "Requires staff confirmation")
    if quote.infants:
        item("Infant per-person fare", format_money(quote.infant_unit_price, quote.currency) if quote.infant_unit_price is not None else "Requires staff confirmation")
    if quote.subtotal_amount is None:
        item("Total", "Not calculated - a traveller fare requires staff confirmation")
    else:
        item("Subtotal", format_money(quote.subtotal_amount, quote.currency))
        if quote.gst_rate_percent is None:
            item("GST", "Unspecified; confirm treatment with staff")
            item("Total", f"{format_money(quote.total_amount, quote.currency)} before GST")
        else:
            item("GST added", f"{quote.gst_rate_percent}% - {format_money(quote.gst_amount or Decimal('0'), quote.currency)}")
            item("Quotation total", format_money(quote.total_amount, quote.currency))
    if quote.price_source == "manual" or quote.child_unit_price is not None or quote.infant_unit_price is not None:
        item("Price review", "Staff-entered fares confirmed by staff" if quote.staff_price_confirmed else "Staff price confirmation required")
    lines.append(("", False))

    for label, value in (("Inclusions", quote.inclusions), ("Exclusions", quote.exclusions), ("Payment terms", quote.payment_terms)):
        if value:
            section(label)
            lines.extend((line, False) for line in _wrap(value))
            lines.append(("", False))

    lines.append(("This quotation is not a booking confirmation. Package availability and final terms must be confirmed by YatraNest staff.", False))
    return lines


def render_quotation_pdf(quote: Quotation) -> bytes:
    """Return a paginated, branded PDF without third-party PDF dependencies."""
    logical = _quote_lines(quote)
    page_groups = [logical[index:index + 43] for index in range(0, len(logical), 43)] or [[]]
    objects: list[bytes] = []
    pages_object_id = 2
    font_regular_id = 3
    font_bold_id = 4
    page_ids: list[int] = []

    def add_object(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    add_object(b"<< /Type /Catalog /Pages 2 0 R >>")
    add_object(b"")  # Pages tree gets filled after page objects are known.
    add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    for page_index, group in enumerate(page_groups):
        commands = [
            "q 0.07 0.22 0.16 rg 0 752 595 90 re f Q",
            "BT /F2 25 Tf 48 797 Td (YatraNest) Tj ET",
            f"BT /F1 11 Tf 48 775 Td (Quotation {page_index + 1} of {len(page_groups)}) Tj ET",
            "BT /F2 17 Tf 48 716 Td (Travel quotation) Tj ET",
            "BT /F1 10 Tf 48 697 Td (Prepared for " + _pdf_text(quote.customer_name_snapshot) + ") Tj ET",
        ]
        y = 665
        for text, bold in group:
            if y < 54:
                break
            if text:
                escaped = _pdf_text(text)
                font = "F2" if bold else "F1"
                size = 11 if bold else 9.5
                commands.append(f"BT /{font} {size} Tf 48 {y} Td ({escaped}) Tj ET")
            y -= 14 if bold else 12
        commands.extend([
            "0.72 0.74 0.69 RG 48 42 m 547 42 l S",
            "BT /F1 8 Tf 48 28 Td (YatraNest | Prepared for staff review | This document does not confirm a booking.) Tj ET",
        ])
        stream = "\n".join(commands).encode("ascii", "replace")
        content_id = add_object(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_id = add_object(
            f"<< /Type /Page /Parent {pages_object_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode("ascii")
        )
        page_ids.append(page_id)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[pages_object_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
    result = bytearray(b"%PDF-1.4\n%YatraNest\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii"))
        result.extend(obj)
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    )
    return bytes(result)
