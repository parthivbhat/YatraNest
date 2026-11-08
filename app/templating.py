"""Shared Jinja2 template environment and display filters."""

from calendar import month_name
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.destination_media import package_destination_media


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


def format_money(value: Decimal | int | float, currency: str = "INR") -> str:
    """Format an amount with Indian digit grouping and its stored currency."""
    amount = Decimal(str(value))
    fixed = f"{amount:.2f}"
    whole, fraction = fixed.split(".")
    if len(whole) > 3:
        trailing = whole[-3:]
        leading = whole[:-3]
        groups: list[str] = []
        while leading:
            groups.insert(0, leading[-2:])
            leading = leading[:-2]
        whole = f"{','.join(groups)},{trailing}"
    rendered = whole if fraction == "00" else f"{whole}.{fraction}"
    if currency == "INR":
        return f"₹{rendered}"
    return f"{currency} {rendered}"


templates.env.filters["money"] = format_money
templates.env.filters["month_name"] = lambda month: month_name[month]
templates.env.globals["package_destination_media"] = package_destination_media
